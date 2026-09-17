import asyncio
import logging
import re
from urllib.parse import urlparse

from telegram_media import parse_telegram_message_link

logger = logging.getLogger("telegram-remote")

CHUNK_BYTES = 512 * 1024
RETRY_CHUNK_BYTES = 128 * 1024
ALIGN_BYTES = 4096
MAX_RANGE_BYTES = 8 * 1024 * 1024


def _video_document(message):
    """Return a Telegram/Telethon document when the message contains one."""
    document = getattr(message, "document", None)
    if document is not None:
        return document

    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is not None:
        return document

    return None


def _is_usable_video_message(message):
    document = _video_document(message)
    if document is None:
        return False

    mime = (getattr(document, "mime_type", "") or "").lower()
    if mime.startswith("video/"):
        return True

    for attribute in getattr(document, "attributes", []) or []:
        if attribute.__class__.__name__.lower() == "documentattributevideo":
            return True
        if getattr(attribute, "duration", None) is not None and hasattr(attribute, "w") and hasattr(attribute, "h"):
            return True

    return False


def _video_duration(message):
    document = _video_document(message)
    if document is None:
        return None
    for attribute in getattr(document, "attributes", []) or []:
        duration = getattr(attribute, "duration", None)
        if duration:
            return float(duration)
    return None


def _media_size(message):
    document = _video_document(message)
    if document is not None:
        size = getattr(document, "size", None)
        if size:
            return int(size)

    size = getattr(getattr(message, "file", None), "size", None)
    return int(size) if size else None


async def get_telegram_video_info(client, chat, message_id):
    try:
        message = await client.get_messages(chat, ids=message_id)
    except Exception as exc:
        logger.exception("Telegram source lookup failed chat=%r message_id=%r", chat, message_id)
        raise RuntimeError(
            f"Telegram source message access failed (chat={chat}, message={message_id}): {exc}"
        ) from exc

    if not message:
        raise RuntimeError(
            f"Telegram source message nahi mila (chat={chat}, message={message_id}). "
            "USER_SESSION account ko is chat/message ka access nahi hai."
        )

    if not _is_usable_video_message(message):
        document = _video_document(message)
        mime = getattr(document, "mime_type", None) if document else None
        media_type = type(getattr(message, "media", None)).__name__
        logger.error(
            "Telegram source is not a usable video: chat=%r message_id=%r message_type=%s media_type=%s document=%s mime=%r",
            chat,
            message_id,
            type(message).__name__,
            media_type,
            type(document).__name__ if document else None,
            mime,
        )
        raise RuntimeError(
            "Telegram source message me usable video nahi hai "
            f"(chat={chat}, message={message_id}, media={media_type}, mime={mime!r})."
        )

    duration = _video_duration(message)
    size = _media_size(message)
    if not duration or duration <= 0:
        raise RuntimeError("Telegram source video duration nahi mila.")
    if not size or size <= 0:
        raise RuntimeError("Telegram source ka media size nahi mila.")
    return message, duration, size


def _parse_range(header, size):
    if not header:
        return 0, min(size - 1, MAX_RANGE_BYTES - 1)

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


async def _read_range_once(client, media, start, end, request_size):
    aligned_start = (start // ALIGN_BYTES) * ALIGN_BYTES
    needed = end - aligned_start + 1
    result = bytearray()

    async for chunk in client.iter_download(
        media,
        offset=aligned_start,
        request_size=request_size,
        chunk_size=request_size,
    ):
        if not chunk:
            break
        result.extend(bytes(chunk))
        if len(result) >= needed:
            break

    trim_start = start - aligned_start
    return bytes(result[trim_start : trim_start + (end - start + 1)])


async def _read_range(client, media, start, end):
    """Read an exact byte range from Telegram with a smaller-chunk retry."""
    if end < start:
        return b""

    expected = end - start + 1
    last_error = None
    for request_size in (CHUNK_BYTES, RETRY_CHUNK_BYTES):
        try:
            payload = await _read_range_once(client, media, start, end, request_size)
            if len(payload) == expected:
                return payload
            last_error = RuntimeError(
                f"Telegram returned {len(payload)} bytes, expected {expected}."
            )
            logger.warning(
                "Short Telegram range read start=%s end=%s got=%s expected=%s request=%s",
                start, end, len(payload), expected, request_size,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Telegram range read failed start=%s end=%s request=%s: %s",
                start, end, request_size, exc,
            )

    raise last_error or RuntimeError("Telegram range read failed.")


class TelegramRangeServer:
    """Local seekable HTTP facade over a Telegram media file."""

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
            self._handle, host="127.0.0.1", port=0, limit=64 * 1024,
        )
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/video.mp4"
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
            if method not in {"GET", "HEAD"} or urlparse(path).path != "/video.mp4":
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

            content_length = end - start + 1
            response_headers = [
                "HTTP/1.1 206 Partial Content",
                "Content-Type: video/mp4",
                f"Content-Length: {content_length}",
                "Accept-Ranges: bytes",
                f"Content-Range: bytes {start}-{end}/{self.size}",
                "Cache-Control: no-store",
                "Connection: close",
            ]
            writer.write(("\r\n".join(response_headers) + "\r\n\r\n").encode())
            await writer.drain()

            if method == "HEAD":
                return

            payload = await _read_range(self.client, self.media, start, end)
            if len(payload) != content_length:
                raise RuntimeError(
                    f"Telegram returned {len(payload)} bytes, expected {content_length}."
                )
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
            ).encode() + body
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


async def open_telegram_message_range_server(client, chat, message_id):
    """Open a range server for a video sent directly to the bot chat."""
    message, duration, size = await get_telegram_video_info(client, chat, message_id)
    server = TelegramRangeServer(client, message, duration, size)
    await server.start()
    return server


async def open_telegram_range_server(client, source_url):
    chat, message_id = parse_telegram_message_link(source_url)
    return await open_telegram_message_range_server(client, chat, message_id)


async def targeted_episode_window(client, source_url, user_id, target_time, window_seconds=8.0):
    server = await open_telegram_range_server(client, source_url)
    return {
        "url": server.url,
        "server": server,
        "duration": server.duration,
        "size": server.size,
        "estimated_time": max(0.0, min(float(target_time), server.duration)),
        "window_seconds": window_seconds,
    }
