import asyncio
import logging
import re
from urllib.parse import urlparse

from telegram_media import parse_telegram_message_link, is_video_message

logger = logging.getLogger("telegram-remote")

CHUNK_BYTES = 512 * 1024
ALIGN_BYTES = 4096
MAX_RANGE_BYTES = 8 * 1024 * 1024


def _video_duration(message):
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is None:
        return None
    for attribute in getattr(document, "attributes", []) or []:
        duration = getattr(attribute, "duration", None)
        if duration:
            return float(duration)
    return None


def _media_size(message):
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    size = getattr(document, "size", None)
    if size:
        return int(size)
    size = getattr(getattr(message, "file", None), "size", None)
    return int(size) if size else None


async def get_telegram_video_info(client, chat, message_id):
    message = await client.get_messages(chat, ids=message_id)
    if not message or not is_video_message(message):
        raise RuntimeError("Telegram source message me usable video nahi hai.")
    duration = _video_duration(message)
    size = _media_size(message)
    if not duration or duration <= 0:
        raise RuntimeError("Telegram source video duration nahi mila.")
    if not size or size <= 0:
        raise RuntimeError("Telegram source ka media size nahi mila.")
    return message, duration, size


def _parse_range(header, size):
    if not header:
        return 0, min(size, MAX_RANGE_BYTES - 1)
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
    if not match:
        raise ValueError("Unsupported Range header")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else size - 1
    if start >= size:
        raise ValueError("Range starts beyond file")
    end = min(end, size - 1)
    if end < start:
        raise ValueError("Invalid byte range")
    if end - start + 1 > MAX_RANGE_BYTES:
        end = start + MAX_RANGE_BYTES - 1
    return start, end


async def _read_range(client, media, start, end):
    """Read an exact byte range without creating a sparse fake media file."""
    if end < start:
        return b""
    aligned_start = (start // ALIGN_BYTES) * ALIGN_BYTES
    needed = end - aligned_start + 1
    result = bytearray()
    async for chunk in client.iter_download(
        media,
        offset=aligned_start,
        request_size=CHUNK_BYTES,
        chunk_size=CHUNK_BYTES,
    ):
        if not chunk:
            break
        result.extend(bytes(chunk))
        if len(result) >= needed:
            break
    trim_start = start - aligned_start
    return bytes(result[trim_start : trim_start + (end - start + 1)])


class TelegramRangeServer:
    """Local seekable HTTP facade over a Telegram media file.

    FFmpeg can use normal HTTP Range requests against this server. Telegram only
    receives the byte ranges FFmpeg actually seeks to, so the full episode is
    never materialized locally just to inspect a small scene.
    """

    def __init__(self, client, message, duration, size):
        self.client = client
        self.message = message
        self.media = getattr(message, "media", None)
        self.duration = duration
        self.size = size
        self.server = None
        self.url = None

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle,
            host="127.0.0.1",
            port=0,
            limit=64 * 1024,
        )
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/video"
        return self.url

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            first, *header_lines = request.decode("iso-8859-1").split("\r\n")
            parts = first.split()
            if len(parts) < 2:
                raise ValueError("Malformed HTTP request")
            method, path = parts[0], parts[1]
            if method not in {"GET", "HEAD"} or urlparse(path).path != "/video":
                await self._send_error(writer, 404, "Not Found")
                return

            headers = {}
            for line in header_lines:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()

            try:
                start, end = _parse_range(headers.get("range"), self.size)
            except ValueError:
                await self._send_416(writer)
                return

            partial = bool(headers.get("range"))
            content_length = end - start + 1
            response_code = "206 Partial Content" if partial else "200 OK"
            response_headers = [
                f"HTTP/1.1 {response_code}",
                "Content-Type: video/mp4",
                f"Content-Length: {content_length}",
                "Accept-Ranges: bytes",
                "Cache-Control: no-store",
                "Connection: close",
            ]
            if partial:
                response_headers.append(
                    f"Content-Range: bytes {start}-{end}/{self.size}"
                )
            writer.write(("\r\n".join(response_headers) + "\r\n\r\n").encode())
            await writer.drain()

            if method == "HEAD":
                return

            payload = await _read_range(
                self.client,
                self.media,
                start,
                end,
            )
            if not payload:
                return
            writer.write(payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("Remote HTTP range request failed")
            try:
                await self._send_error(writer, 500, "Remote range read failed")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_error(self, writer, code, text):
        body = text.encode()
        writer.write(
            (
                f"HTTP/1.1 {code} {text}\r\n"
                "Content-Type: text/plain\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()

    async def _send_416(self, writer):
        writer.write(
            (
                "HTTP/1.1 416 Range Not Satisfiable\r\n"
                f"Content-Range: bytes */{self.size}\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        await writer.drain()


async def open_telegram_range_server(client, source_url):
    chat, message_id = parse_telegram_message_link(source_url)
    message, duration, size = await get_telegram_video_info(client, chat, message_id)
    server = TelegramRangeServer(client, message, duration, size)
    await server.start()
    return server


async def targeted_episode_window(client, source_url, user_id, target_time, window_seconds=8.0):
    """Compatibility helper retained for callers using the old remote API."""
    server = await open_telegram_range_server(client, source_url)
    return {
        "url": server.url,
        "server": server,
        "duration": server.duration,
        "size": server.size,
        "estimated_time": max(0.0, min(float(target_time), server.duration)),
        "window_seconds": window_seconds,
    }
