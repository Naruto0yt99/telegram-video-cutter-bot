"""Isolated Telegram -> HTTP Range -> FFmpeg proof of concept.

This file is intentionally independent of the production clip/find pipeline.
It uses the existing USER_SESSION from config/.env and a real message from
AnimeNation012. Protocol probes deliberately read only a small prefix before
closing, so they do not intentionally consume an entire large video.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.sessions import SQLiteSession
from config import FFMPEG_BIN, FFPROBE_BIN, SOURCE_CHAT, TELEGRAM_SESSION, TG_API_HASH, TG_API_ID, TEMP_DIR

HOST = os.getenv("POC_HOST", "127.0.0.1")
PORT = int(os.getenv("POC_PORT", "0"))
CHUNK = max(64 * 1024, int(os.getenv("POC_CHUNK_SIZE", "524288")))
MIN_SOURCE_BYTES = max(1, int(os.getenv("POC_MIN_SOURCE_BYTES", str(5 * 1024 * 1024))))
LOOP = asyncio.new_event_loop()
BRIDGE = None

class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.telegram_bytes = 0
        self.telegram_chunks = 0
        self.http = []
    def tg(self, n):
        with self.lock:
            self.telegram_bytes += n
            self.telegram_chunks += 1
    def req(self, item):
        with self.lock:
            self.http.append(item)
    def snap(self):
        with self.lock:
            return self.telegram_bytes, self.telegram_chunks, list(self.http)

M = Metrics()

def parse_range(value: Optional[str], total: int):
    if not value:
        return None
    if not value.lower().startswith("bytes=") or "," in value:
        raise ValueError("single bytes range required")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("bad range")
    a, b = spec.split("-", 1)
    if not a:
        suffix = int(b)
        if suffix <= 0:
            raise ValueError("bad suffix")
        return max(0, total - suffix), total - 1
    start = int(a)
    if start < 0 or start >= total:
        raise IndexError("range outside file")
    end = total - 1 if not b else min(int(b), total - 1)
    if end < start:
        raise ValueError("bad range order")
    return start, end

class Bridge:
    def __init__(self, client, message, total, mime):
        self.client, self.message, self.total, self.mime = client, message, total, mime
        self.media = message.media
    async def stream(self, start, end, writer):
        wanted = end - start + 1
        sent = 0
        async for chunk in self.client.iter_download(self.media, offset=start, request_size=CHUNK, chunk_size=CHUNK):
            if not chunk:
                break
            chunk = chunk[: wanted - sent]
            writer(chunk)
            sent += len(chunk)
            M.tg(len(chunk))
            if sent >= wanted:
                break
        return sent

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *_):
        return
    def send_headers(self, status, length, total, mime, cr=None):
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        if cr:
            self.send_header("Content-Range", cr)
        self.send_header("Connection", "close")
        self.end_headers()
    def do_HEAD(self):
        if self.path != "/stream" or BRIDGE is None:
            self.send_error(404); return
        self.send_headers(200, BRIDGE.total, BRIDGE.total, BRIDGE.mime)
        M.req({"method":"HEAD","status":200,"range":None,"bytes":0})
    def do_GET(self):
        if self.path != "/stream" or BRIDGE is None:
            self.send_error(404); return
        raw = self.headers.get("Range")
        try:
            r = parse_range(raw, BRIDGE.total)
        except (ValueError, IndexError, OverflowError):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{BRIDGE.total}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            M.req({"method":"GET","status":416,"range":raw,"bytes":0})
            return
        if r is None:
            start, end, status, cr = 0, BRIDGE.total - 1, 200, None
        else:
            start, end, status, cr = r[0], r[1], 206, f"bytes {r[0]}-{r[1]}/{BRIDGE.total}"
        sent = 0
        self.send_headers(status, end-start+1, BRIDGE.total, BRIDGE.mime, cr)
        try:
            fut = asyncio.run_coroutine_threadsafe(BRIDGE.stream(start, end, self.wfile.write), LOOP)
            sent = fut.result(timeout=max(30, (end-start+1)/(256*1024)*10))
        except Exception as exc:
            print(f"[HTTP] stream stopped: {exc}", flush=True)
        M.req({"method":"GET","status":status,"range":raw,"resolved":f"{start}-{end}","bytes":sent})
        print(f"[HTTP] {status} range={raw!r} resolved={start}-{end} sent={sent}", flush=True)

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def loop_runner():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()

def submit(value):
    """Run an awaitable on the dedicated Telethon loop; also accept sync results."""
    if not inspect.isawaitable(value):
        return value
    async def wait_any(awaitable):
        return await awaitable
    return asyncio.run_coroutine_threadsafe(wait_any(value), LOOP).result()

def close_client(client):
    """Telethon disconnect() can be sync or awaitable depending on client state/version."""
    try:
        submit(client.disconnect())
    except Exception as exc:
        print(f"[POC] disconnect cleanup warning: {exc}", flush=True)


def size_of(message):
    size = getattr(getattr(message, "file", None), "size", None)
    if size:
        return int(size)
    size = getattr(getattr(getattr(message, "media", None), "document", None), "size", None)
    if size:
        return int(size)
    raise RuntimeError("Telegram media size unavailable")

def mime_of(message):
    return getattr(getattr(message, "file", None), "mime_type", None) or getattr(getattr(getattr(message, "media", None), "document", None), "mime_type", None) or "application/octet-stream"

def video_message(message):
    f = getattr(message, "file", None)
    mime = getattr(f, "mime_type", None) or getattr(getattr(getattr(message, "media", None), "document", None), "mime_type", None) or ""
    name = getattr(f, "name", None) or ""
    return mime.startswith("video/") or bool(re.search(r"\.(mp4|mkv|webm|mov|avi)$", name, re.I))

async def resolve(client, chat, ident):
    if ident.lower() != "latest":
        msg = await client.get_messages(chat, ids=int(ident))
        if not msg or not msg.media:
            raise RuntimeError(f"message {ident} has no media")
        return msg
    async for msg in client.iter_messages(chat, limit=100):
        if not msg or not msg.media or not video_message(msg):
            continue
        size = getattr(getattr(msg, "file", None), "size", None) or 0
        if int(size) < MIN_SOURCE_BYTES:
            continue
        return msg
    raise RuntimeError(f"no recent video >= {MIN_SOURCE_BYTES} bytes found in source chat; pass --message MESSAGE_ID for a specific episode")

def probe_request(url, method="GET", headers=None, body_limit=2048):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(body_limit) if body_limit else b""
            return r.status, dict(r.headers), data
    except urllib.error.HTTPError as e:
        data = e.read(body_limit) if body_limit else b""
        return e.code, dict(e.headers), data

def protocol_tests(url, total):
    status, h, body = probe_request(url, "HEAD", body_limit=0)
    assert status == 200 and h.get("Accept-Ranges") == "bytes" and int(h["Content-Length"]) == total and not body
    print("[POC] HEAD 200: PASS", flush=True)

    status, h, body = probe_request(url, "GET", body_limit=1024)
    assert status == 200 and int(h["Content-Length"]) == total and body
    print("[POC] GET without Range -> 200: PASS (read only prefix)", flush=True)

    status, h, body = probe_request(url, "GET", {"Range":"bytes=0-1023"}, 2048)
    assert status == 206 and len(body) == 1024 and h.get("Content-Range") == f"bytes 0-1023/{total}"
    print("[POC] GET bytes=0-1023 -> 206: PASS", flush=True)

    status, h, body = probe_request(url, "GET", {"Range":"bytes=1024-"}, 1024)
    assert status == 206 and h.get("Content-Range") == f"bytes 1024-{total-1}/{total}" and body
    print("[POC] GET bytes=1024- -> 206: PASS (read only prefix)", flush=True)

    suffix = min(1024, total)
    status, h, body = probe_request(url, "GET", {"Range":f"bytes=-{suffix}"}, 2048)
    assert status == 206 and len(body) == suffix and h.get("Content-Range") == f"bytes {total-suffix}-{total-1}/{total}"
    print("[POC] GET suffix range -> 206: PASS", flush=True)

    status, h, _ = probe_request(url, "GET", {"Range":f"bytes={total}-"}, 0)
    assert status == 416 and h.get("Content-Range") == f"bytes */{total}"
    print("[POC] GET invalid range -> 416: PASS", flush=True)

def ffmpeg_cut(url, seek, duration, out):
    cmd = [FFMPEG_BIN,"-hide_banner","-loglevel","warning","-ss",str(seek),"-i",url,"-t",str(duration),"-c","copy","-y",str(out)]
    print("[FFMPEG] " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        print(p.stderr[-4000:], flush=True)
        raise RuntimeError(f"ffmpeg exit={p.returncode}")

def duration_of(path):
    p = subprocess.run([FFPROBE_BIN,"-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("ffprobe failed")
    return float(p.stdout.strip())

def make_isolated_session():
    """Make a unique SQLiteSession object so sitecustomize cannot reuse a locked path."""
    source = Path(str(TELEGRAM_SESSION))
    source_file = source if source.suffix == ".session" else Path(str(source) + ".session")
    if not source_file.exists():
        raise RuntimeError("Telegram USER_SESSION file was not found")
    temp_dir = Path(tempfile.mkdtemp(prefix="range_poc_session_", dir=str(TEMP_DIR)))
    base = temp_dir / "session"
    shutil.copy2(source_file, Path(str(base) + ".session"))
    return SQLiteSession(str(base)), temp_dir

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default=os.getenv("POC_SOURCE_CHAT", SOURCE_CHAT))
    ap.add_argument("--message", default=os.getenv("POC_SOURCE_MESSAGE_ID", "latest"))
    ap.add_argument("--seek", type=float, default=float(os.getenv("POC_SEEK_SECONDS", "60")))
    ap.add_argument("--duration", type=float, default=float(os.getenv("POC_DURATION_SECONDS", "5")))
    a = ap.parse_args()
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH missing from .env")
    if a.seek < 0 or not 0 < a.duration <= 30:
        raise ValueError("seek >= 0 and duration must be 0<duration<=30")

    out = Path(TEMP_DIR) / "range_proxy_poc_output.mp4"
    if out.exists(): out.unlink()
    session, session_dir = make_isolated_session()
    threading.Thread(target=loop_runner, daemon=True).start()
    client = TelegramClient(session, TG_API_ID, TG_API_HASH)
    submit(client.connect())
    try:
        if not submit(client.is_user_authorized()):
            raise RuntimeError("Telegram USER_SESSION is not authorized")
        msg = submit(resolve(client, a.chat, a.message))
        total, mime = size_of(msg), mime_of(msg)
        print(f"[POC] chat={a.chat} message_id={msg.id}", flush=True)
        print(f"[POC] file={getattr(getattr(msg,'file',None),'name',None) or '(unnamed)'} mime={mime}", flush=True)
        print(f"[POC] source_size={total} bytes ({total/1024/1024:.2f} MiB)", flush=True)
        if total < MIN_SOURCE_BYTES:
            raise RuntimeError(f"selected source is too small ({total} bytes); use a real episode message or raise/lower POC_MIN_SOURCE_BYTES")

        global BRIDGE
        BRIDGE = Bridge(client, msg, total, mime)
        server = Server((HOST, PORT), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://{HOST}:{port}/stream"
        print(f"[POC] proxy={url}", flush=True)
        try:
            protocol_tests(url, total)
            before = M.snap()[0]
            started = time.monotonic()
            ffmpeg_cut(url, a.seek, a.duration, out)
            elapsed = time.monotonic() - started
            after, chunks, requests = M.snap()
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("ffmpeg output missing/empty")
            out_dur = duration_of(out)
            used = after - before
            print("", flush=True)
            print("========== RANGE PROXY POC RESULT ==========" , flush=True)
            print("RESULT: PASS", flush=True)
            print(f"source_bytes={total}", flush=True)
            print(f"ffmpeg_elapsed={elapsed:.2f}s", flush=True)
            print(f"output_bytes={out.stat().st_size}", flush=True)
            print(f"output_duration={out_dur:.3f}s", flush=True)
            print(f"telegram_bytes_during_ffmpeg={used}", flush=True)
            print(f"telegram_bytes_ratio={used/total:.2%}", flush=True)
            print(f"telegram_iter_download_chunks={chunks}", flush=True)
            print("http_requests:", flush=True)
            for item in requests:
                print("  " + json.dumps(item, sort_keys=True), flush=True)
            print("============================================", flush=True)
        finally:
            server.shutdown(); server.server_close()
    finally:
        close_client(client)
        LOOP.call_soon_threadsafe(LOOP.stop)
        if out.exists(): out.unlink()
        shutil.rmtree(session_dir, ignore_errors=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[POC] interrupted", flush=True); sys.exit(130)
    except Exception as e:
        print(f"[POC] FAIL: {e}", flush=True); sys.exit(1)
