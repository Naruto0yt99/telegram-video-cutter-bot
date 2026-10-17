import os
import threading
import time
from datetime import datetime, timezone

import psycopg
from flask import Flask, jsonify, request, abort
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
app = Flask(__name__)

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, connect_timeout=10)

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id BIGSERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                command TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','failed')),
                result TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_status_id_idx ON jobs(status, id)")

def authorized_request():
    supplied = request.headers.get("Authorization", "")
    if not BRIDGE_SECRET or supplied != f"Bearer {BRIDGE_SECRET}":
        abort(401)

@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return jsonify(ok=True, database="connected")
    except Exception:
        return jsonify(ok=False, database="unavailable"), 503

@app.post("/agent/next")
def agent_next():
    authorized_request()
    with db() as conn:
        row = conn.execute("""
            SELECT id, command FROM jobs
            WHERE status='pending' ORDER BY id
            FOR UPDATE SKIP LOCKED LIMIT 1
        """).fetchone()
        if not row:
            return jsonify(job=None)
        job_id, command = row
        conn.execute(
            "UPDATE jobs SET status='running', updated_at=now() WHERE id=%s",
            (job_id,)
        )
        return jsonify(job={"id": job_id, "command": command})

@app.post("/agent/result")
def agent_result():
    authorized_request()
    body = request.get_json(silent=True) or {}
    job_id = body.get("id")
    status = body.get("status")
    result = str(body.get("result", ""))[:12000]
    if not isinstance(job_id, int) or status not in ("done", "failed"):
        return jsonify(error="invalid payload"), 400
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status=%s, result=%s, updated_at=now() "
            "WHERE id=%s AND status='running' RETURNING id",
            (status, result, job_id)
        )
        if not cur.fetchone():
            return jsonify(error="job not found or already completed"), 404
    # Push completed job output to the owner. Previously results were only saved
    # in Postgres, so a queued command could finish successfully with no visible
    # Telegram reply until /status was manually requested.
    if BOT_TOKEN and OWNER_ID:
        try:
            import urllib.request
            message = f"☁️ Job #{job_id} {status.upper()}\\n\\n{result[:3200] or '(no output)'}"
            payload = json.dumps({"chat_id": OWNER_ID, "text": message}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                response.read()
        except Exception as exc:
            # Result is already persisted; notification failure must not lose it.
            print(f"Telegram result notification failed for job #{job_id}: {type(exc).__name__}")
    return jsonify(ok=True)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == OWNER_ID:
        await update.effective_message.reply_text(
            "Cloud queue ready ☁️\n/run <command> — queue a Termux command\n/status — show recent jobs"
        )

async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Not authorized.")
        return
    command = " ".join(context.args).strip()
    if not command:
        await msg.reply_text("Usage: /run <command>")
        return
    if len(command) > 1500:
        await msg.reply_text("Command too long (max 1500 characters).")
        return
    with db() as conn:
        row = conn.execute(
            "INSERT INTO jobs(owner_id, command) VALUES (%s, %s) RETURNING id",
            (OWNER_ID, command)
        ).fetchone()
    await msg.reply_text(f"☁️ Job #{row[0]} queued. Termux will run it when connected.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Not authorized.")
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT id, status, command, result FROM jobs "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
    if not rows:
        await msg.reply_text("No jobs yet.")
        return
    lines = []
    for job_id, status, command, result in rows:
        line = f"#{job_id} [{status}] {command[:100]}"
        if result and status in ("done", "failed"):
            line += f"\n{result[:250]}"
        lines.append(line)
    await msg.reply_text("\n\n".join(lines))

def run_telegram():
    import asyncio
    async def runner():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(CommandHandler("run", run_cmd))
        application.add_handler(CommandHandler("status", status_cmd))
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=False)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
    asyncio.run(runner())

if __name__ == "__main__":
    if DATABASE_URL:
        init_db()
    else:
        print("DATABASE_URL missing; configure Render Postgres connection URL.")
    if BOT_TOKEN and DATABASE_URL and BRIDGE_SECRET:
        threading.Thread(target=run_telegram, daemon=True).start()
    else:
        print("Cloud bot disabled until DATABASE_URL, BRIDGE_SECRET, and TELEGRAM_BOT_TOKEN are configured.")
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
