import io
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from remote_fingerprint import build_fingerprint_index_image


TOPIC_BIND_FILE = Path("data/fingerprint_topic.json")


def _read_topic_id():
    try:
        data = json.loads(TOPIC_BIND_FILE.read_text(encoding="utf-8"))
        value = data.get("message_thread_id")
        return int(value) if value is not None else None
    except Exception:
        return None


def _write_topic_id(chat_id, message_thread_id):
    TOPIC_BIND_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOPIC_BIND_FILE.write_text(
        json.dumps(
            {
                "chat_id": str(chat_id),
                "message_thread_id": int(message_thread_id),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


async def bind_fingerprint_topic(update):
    message = update.effective_message
    chat = update.effective_chat
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is None:
        raise ValueError("Ye command FINGERPRINTS forum topic ke andar bhejo.")
    _write_topic_id(chat.id, thread_id)
    return thread_id


def get_fingerprint_topic_id():
    return _read_topic_id()


async def check_fingerprint_storage(bot: Bot, chat_id: str):
    """Verify bot access to the supergroup used for fingerprint storage."""
    chat = await bot.get_chat(chat_id)
    me = await bot.get_me()
    member = await bot.get_chat_member(chat.id, me.id)

    status = getattr(member, "status", None)
    can_manage_topics = getattr(member, "can_manage_topics", None)
    can_send_messages = getattr(member, "can_send_messages", None)

    topic_id = _read_topic_id()

    return {
        "chat_id": chat.id,
        "chat_title": getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id),
        "chat_username": getattr(chat, "username", None),
        "bot_id": me.id,
        "bot_username": me.username,
        "status": status,
        "can_manage_topics": can_manage_topics,
        "can_send_messages": can_send_messages,
        "topic_id": topic_id,
        "ok": (
            status in {"administrator", "creator"}
            and topic_id is not None
        ),
    }


async def save_fingerprint_json(
    bot: Bot,
    chat_id: str,
    fingerprint: dict,
    filename: str,
    message_thread_id=None,
):
    """Upload one compact fingerprint JSON artifact into the bound forum topic."""
    check = await check_fingerprint_storage(bot, chat_id)
    topic_id = message_thread_id or check.get("topic_id")

    if check["status"] not in {"administrator", "creator"}:
        raise PermissionError(
            f"Fingerprint storage unavailable: status={check['status']}"
        )
    if topic_id is None:
        raise PermissionError(
            "Fingerprint topic not bound. FINGERPRINTS topic me /fingerprint_bind bhejo."
        )

    payload = json.dumps(
        fingerprint,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    bio = io.BytesIO(payload)
    bio.name = filename if filename.endswith(".json") else filename + ".json"

    caption = (
        "🧠 Anime fingerprint\n"
        f"📦 {fingerprint.get('anime', 'Unknown')} "
        f"S{fingerprint.get('season', '?')} E{fingerprint.get('episode', '?')}\n"
        f"🧩 Topic: {topic_id}\n"
        f"🕒 {datetime.now(timezone.utc).isoformat()}"
    )

    return await bot.send_document(
        chat_id=chat_id,
        document=bio,
        caption=caption,
        message_thread_id=int(topic_id),
    )


async def save_fingerprint_pack(bot: Bot, chat_id: str, fingerprint: dict, filename: str, message_thread_id=None):
    check = await check_fingerprint_storage(bot, chat_id)
    topic_id = message_thread_id or check.get("topic_id")
    if check["status"] not in {"administrator", "creator"}:
        raise PermissionError(f"Fingerprint storage unavailable: status={check['status']}")
    if topic_id is None:
        raise PermissionError("Fingerprint topic not bound. FINGERPRINTS topic me /fingerprint_bind bhejo.")

    safe = filename[:-4] if filename.endswith(".zip") else filename
    with tempfile.TemporaryDirectory(prefix="fp_pack_") as tmp:
        root = Path(tmp)
        json_path = root / "fingerprint.json"
        index_path = root / "index.jpg"
        json_path.write_text(json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        build_fingerprint_index_image(fingerprint, index_path, every_seconds=10.0, columns=12)
        zip_path = root / f"{safe}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(json_path, "fingerprint.json")
            zf.write(index_path, "index.jpg")
        payload = zip_path.read_bytes()

    if len(payload) > 50 * 1024 * 1024:
        raise ValueError("Fingerprint pack 50 MB se bada ho gaya.")
    bio = io.BytesIO(payload)
    bio.name = f"{safe}.zip"
    caption = (
        "🧠 Anime fingerprint pack\\n"
        f"📚 {fingerprint.get('anime', 'Unknown')} S{fingerprint.get('season', '?')} E{fingerprint.get('episode', '?')}\\n"
        "🧩 Contains: fingerprint.json + visual index.jpg"
    )
    return await bot.send_document(chat_id=chat_id, document=bio, caption=caption, message_thread_id=int(topic_id))


async def fingerprint_storage_status(bot: Bot, chat_id: str) -> str:
    try:
        info = await check_fingerprint_storage(bot, chat_id)
    except TelegramError as exc:
        return (
            "❌ FINGERPRINT STORAGE\n\n"
            f"Group: {chat_id}\n"
            f"Telegram error: {exc}"
        )

    if info["ok"]:
        return (
            "✅ FINGERPRINT STORAGE READY\n\n"
            f"📦 Group: {info['chat_title']}\n"
            f"🔗 @{info['chat_username'] or 'private'}\n"
            f"🤖 @{info['bot_username'] or info['bot_id']}\n"
            f"👑 Status: {info['status']}\n"
            f"🧩 Topic ID: {info['topic_id']}\n"
            f"🛠️ Manage Topics: {info['can_manage_topics']}\n\n"
            "🧠 Fingerprint JSON files will be uploaded into the bound topic."
        )

    return (
        "⚠️ FINGERPRINT STORAGE NOT READY\n\n"
        f"📦 Group: {info['chat_title']}\n"
        f"🤖 @{info['bot_username'] or info['bot_id']}\n"
        f"👤 Status: {info['status']}\n"
        f"🛠️ Manage Topics: {info['can_manage_topics']}\n"
        f"💬 Can send messages: {info['can_send_messages']}\n"
        f"🧩 Topic ID: {info['topic_id'] or 'NOT BOUND'}\n\n"
        "FINGERPRINTS topic ke andar /fingerprint_bind bhejo."
    )


async def save_fingerprint_artifacts(
    bot: Bot,
    chat_id: str,
    fingerprint: dict,
    json_filename: str,
    index_path,
    message_thread_id=None,
):
    """Store one episode fingerprint as separate JSON, PDF and visual-index messages."""
    from pathlib import Path
    import textwrap

    check = await check_fingerprint_storage(bot, chat_id)
    topic_id = message_thread_id or check.get("topic_id")
    if check["status"] not in {"administrator", "creator"}:
        raise PermissionError(f"Fingerprint storage unavailable: status={check['status']}")
    if topic_id is None:
        raise PermissionError("Fingerprint topic not bound. FINGERPRINTS topic me /fingerprint_bind bhejo.")

    root = Path(index_path).parent
    safe = Path(json_filename).stem
    pdf_path = root / f"{safe}.pdf"

    # Human-readable PDF: includes the full machine JSON in wrapped monospace text,
    # so the user can inspect exactly what is stored for FIND.
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted, PageBreak
    from reportlab.lib.units import mm

    raw_json = json.dumps(
        {k: v for k, v in fingerprint.items() if not k.startswith("_")},
        ensure_ascii=False,
        indent=2,
    )
    wrapped = []
    for line in raw_json.splitlines():
        if len(line) <= 105:
            wrapped.append(line)
        else:
            wrapped.extend(textwrap.wrap(line, width=105, break_long_words=True, break_on_hyphens=False))
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    mono = ParagraphStyle(
        "FingerprintMono",
        parent=styles["Code"],
        fontName="Courier",
        fontSize=6.2,
        leading=7.2,
        alignment=TA_LEFT,
        textColor=colors.black,
    )
    story = [
        Paragraph("AnimeClipCutter — Episode Fingerprint", styles["Title"]),
        Spacer(1, 5 * mm),
        Paragraph(
            f"<b>{fingerprint.get('anime','Unknown')} S{fingerprint.get('season','?')} E{fingerprint.get('episode','?')}</b>",
            styles["Heading2"],
        ),
        Paragraph(
            f"Duration: {fingerprint.get('duration','?')}s &nbsp;&nbsp; "
            f"Sample interval: {fingerprint.get('sample_every','?')}s &nbsp;&nbsp; "
            f"Samples: {len(fingerprint.get('times', []))}",
            styles["BodyText"],
        ),
        Spacer(1, 3 * mm),
        Paragraph("Below is the stored machine fingerprint JSON:", styles["BodyText"]),
        Spacer(1, 2 * mm),
        Preformatted("\n".join(wrapped), mono),
    ]
    doc.build(story)

    payload = json.dumps(
        {k: v for k, v in fingerprint.items() if not k.startswith("_")},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    bio = io.BytesIO(payload)
    bio.name = json_filename if json_filename.endswith(".json") else json_filename + ".json"
    json_msg = await bot.send_document(
        chat_id=chat_id,
        document=bio,
        caption=(
            "🧠 RAW FINGERPRINT\n"
            f"📚 {fingerprint.get('anime','Unknown')} S{fingerprint.get('season','?')} E{fingerprint.get('episode','?')}"
        ),
        message_thread_id=int(topic_id),
    )

    with pdf_path.open("rb") as handle:
        pdf_msg = await bot.send_document(
            chat_id=chat_id,
            document=handle,
            caption=(
                "📄 FINGERPRINT PDF\n"
                f"📚 {fingerprint.get('anime','Unknown')} S{fingerprint.get('season','?')} E{fingerprint.get('episode','?')}"
            ),
            message_thread_id=int(topic_id),
        )

    index_path = Path(index_path)
    if index_path.stat().st_size <= 10 * 1024 * 1024:
        with index_path.open("rb") as handle:
            index_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=handle,
                caption=(
                    "🖼️ VISUAL INDEX\n"
                    f"📚 {fingerprint.get('anime','Unknown')} S{fingerprint.get('season','?')} E{fingerprint.get('episode','?')}\n"
                    f"⏱️ One real frame every 2 seconds"
                ),
                message_thread_id=int(topic_id),
            )
    else:
        with index_path.open("rb") as handle:
            index_msg = await bot.send_document(
                chat_id=chat_id,
                document=handle,
                caption=(
                    "🖼️ VISUAL INDEX (document)\n"
                    f"📚 {fingerprint.get('anime','Unknown')} S{fingerprint.get('season','?')} E{fingerprint.get('episode','?')}"
                ),
                message_thread_id=int(topic_id),
            )

    return {
        "json": json_msg,
        "pdf": pdf_msg,
        "index": index_msg,
    }
