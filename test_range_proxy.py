"""Isolated PoC: Telegram media -> HTTP Range proxy -> FFmpeg.

This file intentionally does NOT modify or import the production clip pipeline.
It proves the transport mechanism before any production rewiring.

Required environment:
  TG_API_ID / TG_API_HASH / TELEGRAM_SESSION (loaded by config.py/.env)

Optional environment:
  POC_SOURCE_CHAT=AnimeNation012
  POC_SOURCE_MESSAGE_ID=latest
  POC_SEEK_SECONDS=60
  POC_DURATION_SECONDS=5
  POC_HOST=127.0.0.1
  POC_PORT=0
  POC_CHUNK_SIZE=524288

The test:
  1. Resolves a real Telegram video/document message.
  2. Starts a local HTTP/1.1 Range server bound to that one message.
  3. Verifies HEAD, normal GET, 206 ranges, suffix/open-ended ranges and 416.
  4. Runs ffmpeg -ss <seek> -i http://127.0.0.1:<port>/stream -t <duration> -c copy.
  5. Reports HTTP Range requests, Telegram bytes yielded, source size and output duration.

No production modules are changed by this PoC.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

from config import (
    FFMPEG_BIN,
    SOURCE_CHAT,
    TELEGRAM_SESSION,
    TG_API_HASH,
    TG_API_ID,
    TEMP_DIR,
)


HOST = os.getenv("POC_HOST", "127.0.0.1")
PORT = int(os.getenv("POC_PORT", "0"))
CHUNK_SIZE = max(64 * 1024, int(os.getenv("POC_CHUNK_SIZE", str(512 * 1024))))


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.http_requests: list[dict] = []
        self.telegram_bytes = 0
        self.telegram_calls = 0
        self.range_failures = 0

    def add_http(self, item: dict) -> None:
        with self.lock:
            self.http_requests.append(item)

    def add_telegram(self, amount: int) -> None:
        with self.lock:
            self.telegram_bytes += amount
            self.telegram_calls += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "http_requests": list(self.http_requests),
                "telegram_bytes": self.telegram_bytes,
                "telegram_calls": self.telegram_calls,
                "range_failures": self.range_failures,
            }


METRICS = Metrics()


def parse_range(value: Optional[str], total: int) -> Optional[tuple[int, int]]:
    """Return one inclusive byte range, or None for no Range header."""
    if not value:
        return None
    if not value.lower().startswith("bytes="):
        raise ValueError("unsupported Range unit")
    spec = value[6:].strip()
    if "," in spec:
        raise ValueError("multi-range is intentionally not implemented in PoC")
    if "-" not in spec:
        raise ValueError("invalid Range syntax")
    start_text, end_text = spec.split("-", 1)
    if not start_text and not end_text:
        raise ValueError("empty Range")

    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(start_text)
        if start < 0 or start >= total:
            raise IndexError("range start outside file")
        if end_text:
            end = int(end_text)
            if end < start:
                raise ValueError("range end before start")
            end = min(end, total - 1)
        else:
            end = total - 1
    return start, end


class TelegramBridge:
    def __init__(self, client: TelegramClient, chat, message, total: int, content_type: str):
        self.client = client
        self.chat = chat
        self.message = message
        self.total = total
        self.content_type = content_type or "application/octet-stream"
        self.media = message.media

    async def read_range(self, start: int, end: int, writer) -> int:
        wanted = end - start + 1
        sent = 0
        async for chunk in self.client.iter_download(
            self.media,
            offset=start,
            request_size=CHUNK_SIZE,
            chunk_size=CHUNK_SIZE,
        ):
            if not chunk:
                break
            remaining = wanted - sent
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            writer(chunk)
            sent += len(chunk)
            METRICS.add_telegram(len(chunk))
            if sent >= wanted:
                break
        return sent


BRIDGE: Optional[TelegramBridge] = None


class RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Keep the useful structured range log below; suppress default noisy log.
        return

    def _headers(self, status: int, total: int, length: int, content_type: str, content_range: Optional[str] = None):
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if content_range:
            self.send_header("Content-Range", content_range)
        self.send_header("Connection", "close")
        self.end_headers()

    def do_HEAD(self):
        if self.path != "/stream" or BRIDGE is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._headers(HTTPStatus.OK, BRIDGE.total, BRIDGE.total, BRIDGE.content_type)
        METRICS.add_http({"method": "HEAD", "status": 200, "range": None, "bytes": 0})

    def do_GET(self):
        if self.path != "/stream" or BRIDGE is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        total = BRIDGE.total
        raw_range = self.headers.get("Range")

        if raw_range is None:
            start, end, status = 0, total - 1, 200
            content_range = None
        else:
            try:
                parsed = parse_range(raw_range, total)
                if parsed is None:
                    raise ValueError("missing range")
                start, end = parsed
                status = 206
                content_range = f"bytes {start}-{end}/{total}"
            except (ValueError, IndexError, OverflowError):
                with METRICS.lock:
                    METRICS.range_failures += 1
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                METRICS.add_http({"method": "GET", "status": 416, "range": raw_range, "bytes": 0})
                return

        length = end - start + 1
        self._headers(status, total, length, BRIDGE.content_type, content_range)
        sent = 0
        try:
            # Telethon is async, while BaseHTTPRequestHandler is synchronous.
            # Run the async range fetch on the dedicated Telethon event loop.
            future = asyncio.run_coroutine_threadsafe(
                BRIDGE.read_range(start, end, self.wfile.write),
                TELETHON_LOOP,
            )
            sent = future.result(timeout=max(30.0, length / (256 * 1024) * 10))
        except Exception as exc:
            print(f"[HTTP] stream error range={start}-{end}: {exc}", flush=True)
        finally:
            METRICS.add_http(
                {
                    "method": "GET",
                    "status": status,
                    "range": raw_range,
                    "resolved": f"{start}-{end}",
                    "bytes": sent,
                }
            )
            print(
                f"[HTTP] GET status={status} range={raw_range!r} resolved={start}-{end} sent={sent}",
                flush=True,
            )


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


TELETHON_LOOP = asyncio.new_event_loop()


def run_telethon_loop():
    asyncio.set_event_loop(TELETHON_LOOP)
    TELETHON_LOOP.run_forever()


def submit(coro):
    return asyncio.run_coroutine_threadsafe(coro, TELETHON_LOOP).result()


def message_size(message) -> int:
    size = getattr(getattr(message, "file", None), "size", None)
    if size:
        return int(size)
    document = getattr(getattr(message, "media", None), "document", None)
    size = getattr(document, "size", None)
    if size:
        return int(size)
    raise RuntimeError("Telegram message has no readable media size")


def content_type(message) -> str:
    mime = getattr(getattr(message, "file", None), "mime_type", None)
    if mime:
        return mime
    document = getattr(getattr(message, "media", None), "document", None)
    mime = getattr(document, "mime_type", None)
    return mime or "application/octet-stream"


def is_video_message(message) -> bool:
    file_obj = getattr(message, "file", None)
    mime = getattr(file_obj, "mime_type", None) or ""
    if mime.startswith("video/"):
        return True
    document = getattr(getattr(message, "media", None), "document", None)
    mime = getattr(document, "mime_type", None) or ""
    if mime.startswith("video/"):
        return True
    return bool(getattr(file_obj, "name", None) and re.search(r"\.(mp4|mkv|webm|mov|avi)$", file_obj.name, re.I))


async def resolve_message(client: TelegramClient, chat, message_id: str):
    if message_id.lower() != "latest":
        message = await client.get_messages(chat, ids=int(message_id))
        if not message or not message.media:
            raise RuntimeError(f"Message {message_id} has no media")
        return message

    async for message in client.iter_messages(chat, limit=100):
        if message and message.media and is_video_message(message):
            return message
    raise RuntimeError("Could not find a recent video/document in source chat")


def http_request(method: str, url: str, headers: Optional[dict] = None):
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def validate_protocol(base_url: str, total: int):
    print("[POC] Validating HTTP protocol...", flush=True)
    status, headers, body = http_request("HEAD", base_url)
    assert status == 200, f"HEAD expected 200, got {status}"
    assert headers.get("Accept-Ranges") == "bytes"
    assert int(headers["Content-Length"]) == total
    assert body == b""
    print("[POC] HEAD 200: PASS", flush=True)

    status, headers, body = http_request("GET", base_url, {"Range": "bytes=0-1023"})
    assert status == 206, f"range GET expected 206, got {status}"
    assert len(body) == 1024
    assert headers.get("Content-Range") == f"bytes 0-1023/{total}"
    print("[POC] GET bytes=0-1023 -> 206: PASS", flush=True)

    status, headers, body = http_request("GET", base_url, {"Range": "bytes=1024-"})
    assert status == 206
    assert len(body) == total - 1024
    assert headers.get("Content-Range") == f"bytes 1024-{total - 1}/{total}"
    print("[POC] GET bytes=1024- -> 206: PASS", flush=True)

    suffix = min(1024, total)
    status, headers, body = http_request("GET", base_url, {"Range": f"bytes=-{suffix}"})
    assert status == 206
    assert len(body) == suffix
    assert headers.get("Content-Range") == f"bytes {total - suffix}-{total - 1}/{total}"
    print("[POC] GET suffix range -> 206: PASS", flush=True)

    status, headers, body = http_request("GET", base_url, {"Range": f"bytes={total}-"})
    assert status == 416
    assert headers.get("Content-Range") == f"bytes */{total}"
    print("[POC] GET invalid range -> 416: PASS", flush=True)


def run_ffmpeg(url: str, seek: float, duration: float, output: Path):
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-ss",
        str(seek),
        "-i",
        url,
        "-t",
        str(duration),
        "-c",
        "copy",
        "-y",
        str(output),
    ]
    print("[FFMPEG] " + " ".join(cmd), flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-4000:], flush=True)
        raise RuntimeError(f"FFmpeg failed with exit code {result.returncode}")


def probe_duration(output: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("ffprobe could not read the output")
    return float(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser(description="Telegram Range proxy PoC")
    parser.add_argument("--chat", default=os.getenv("POC_SOURCE_CHAT", SOURCE_CHAT))
    parser.add_argument("--message", default=os.getenv("POC_SOURCE_MESSAGE_ID", "latest"))
    parser.add_argument("--seek", type=float, default=float(os.getenv("POC_SEEK_SECONDS", "60")))
    parser.add_argument("--duration", type=float, default=float(os.getenv("POC_DURATION_SECONDS", "5")))
    args = parser.parse_args()

    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID/TG_API_HASH are missing from .env")

    if args.duration <= 0 or args.duration > 30:
        raise ValueError("PoC duration must be between 0 and 30 seconds")
    if args.seek < 0:
        raise ValueError("Seek must be >= 0")

    output = Path(TEMP_DIR) / "range_proxy_poc_output.mp4"
    if output.exists():
        output.unlink()

    telethon_thread = threading.Thread(target=run_telethon_loop, name="telethon-poc-loop", daemon=True)
    telethon_thread.start()

    client = TelegramClient(TELEGRAM_SESSION, TG_API_ID, TG_API_HASH)
    submit(client.connect())
    if not submit(client.is_user_authorized()):
        raise RuntimeError("Telegram USER_SESSION is not authorized")

    message = submit(resolve_message(client, args.chat, args.message))
    total = message_size(message)
    mime = content_type(message)
    name = getattr(getattr(message, "file", None), "name", None) or "(unnamed)"

    print(f"[POC] Source chat: {args.chat}", flush=True)
    print(f"[POC] Source message id: {message.id}", flush=True)
    print(f"[POC] File name: {name}", flush=True)
    print(f"[POC] MIME: {mime}", flush=True)
    print(f"[POC] Telegram file size: {total} bytes ({total / 1024 / 1024:.2f} MiB)", flush=True)

    global BRIDGE
    BRIDGE = TelegramBridge(client, args.chat, message, total, mime)

    server = Server((HOST, PORT), RangeHandler)
    actual_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, name="range-http-server", daemon=True)
    server_thread.start()
    base_url = f"http://{HOST}:{actual_port}/stream"

    print(f"[POC] Range proxy: {base_url}", flush=True)

    try:
        validate_protocol(base_url, total)

        before = METRICS.snapshot()["telegram_bytes"]
        started = time.monotonic()
        run_ffmpeg(base_url, args.seek, args.duration, output)
        elapsed = time.monotonic() - started
        after = METRICS.snapshot()["telegram_bytes"]

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced no usable output")
        out_duration = probe_duration(output)
        metrics = METRICS.snapshot()
        ffmpeg_telegram_bytes = after - before

        print("", flush=True)
        print("========== RANGE PROXY POC RESULT ==========" , flush=True)
        print(f"RESULT: PASS", flush=True)
        print(f"Source bytes: {total}", flush=True)
        print(f"FFmpeg elapsed: {elapsed:.2f}s", flush=True)
        print(f"Output bytes: {output.stat().st_size}", flush=True)
        print(f"Output duration: {out_duration:.3f}s", flush=True)
        print(f"Telegram bytes during FFmpeg: {ffmpeg_telegram_bytes}", flush=True)
        print(f"Telegram bytes / source size: {ffmpeg_telegram_bytes / total:.2%}", flush=True)
        print(f"Telegram iter_download chunks: {metrics['telegram_calls']}", flush=True)
        print("HTTP requests seen:", flush=True)
        for item in metrics["http_requests"]:
            print("  " + json.dumps(item, sort_keys=True), flush=True)
        print("============================================", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        submit(client.disconnect())
        TELETHON_LOOP.call_soon_threadsafe(TELETHON_LOOP.stop)
        if output.exists():
            output.unlink()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[POC] Interrupted", flush=True)
        sys.exit(130)
    except Exception as exc:
        print(f"[POC] FAIL: {exc}", flush=True)
        sys.exit(1)
