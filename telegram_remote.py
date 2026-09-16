import logging
import math
import os
from pathlib import Path

from config import TEMP_DIR
from telegram_media import parse_telegram_message_link, is_video_message

logger = logging.getLogger("telegram-remote")


HEAD_BYTES = 2 * 1024 * 1024
TAIL_BYTES = 2 * 1024 * 1024
WINDOW_BYTES = 16 * 1024 * 1024
MAX_WINDOW_BYTES = 32 * 1024 * 1024


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


async def _write_range(client, media, path, offset, limit):
    if limit <= 0:
        return 0

    written = 0
    request_size = 512 * 1024

    async for chunk in client.iter_download(
        media,
        offset=max(0, int(offset)),
        limit=int(limit),
        request_size=request_size,
    ):
        if not chunk:
            continue
        with path.open("r+b") as handle:
            handle.seek(int(offset) + written)
            handle.write(chunk)
        written += len(chunk)

    return written


async def download_sparse_window(
    client,
    chat,
    message_id,
    user_id,
    target_time,
    window_seconds=8.0,
):
    """
    Build a sparse local media file containing only the file head, tail,
    and a byte window around the estimated timestamp.

    Telegram is still used as the source; the complete episode is never
    downloaded. FFmpeg can seek against the sparse file when the container
    metadata is usable from the retained head/tail data.
    """
    message = await client.get_messages(chat, ids=message_id)
    if not message or not is_video_message(message):
        raise RuntimeError("Telegram source message me usable video nahi hai.")

    media = getattr(message, "media", None)
    size = _media_size(message)
    duration = _video_duration(message)

    if media is None or size is None or size <= 0:
        raise RuntimeError("Telegram source ka media size nahi mila.")
    if not duration or duration <= 0:
        raise RuntimeError("Telegram source video duration nahi mila.")

    ratio = min(1.0, max(0.0, float(target_time) / duration))
    center = int(size * ratio)

    # Give the candidate enough room for seeking/keyframes. The caller can
    # request a wider second pass if FFmpeg cannot decode the first window.
    desired = int(max(WINDOW_BYTES, size * min(0.08, window_seconds / duration)))
    desired = min(MAX_WINDOW_BYTES, max(WINDOW_BYTES, desired))

    start = max(0, center - desired // 2)
    end = min(size, start + desired)
    start = max(0, end - desired)

    root = Path(TEMP_DIR) / str(user_id) / "remote_windows"
    root.mkdir(parents=True, exist_ok=True)
    safe_name = f"remote_{message_id}_{center}_{desired}.mp4"
    path = root / safe_name

    # Sparse allocation: logical size is the Telegram file size, physical
    # storage is only the ranges written below.
    with path.open("wb") as handle:
        handle.truncate(size)

    try:
        await _write_range(client, media, path, 0, min(HEAD_BYTES, size))

        tail_start = max(0, size - TAIL_BYTES)
        await _write_range(client, media, path, tail_start, size - tail_start)

        await _write_range(client, media, path, start, end - start)

        return {
            "path": path,
            "duration": duration,
            "size": size,
            "window_start_byte": start,
            "window_end_byte": end,
            "estimated_time": ratio * duration,
            "message": message,
        }
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def targeted_episode_window(
    client,
    source_url,
    user_id,
    target_time,
    window_seconds=8.0,
):
    chat, message_id = parse_telegram_message_link(source_url)
    return await download_sparse_window(
        client,
        chat,
        message_id,
        user_id,
        target_time,
        window_seconds=window_seconds,
    )
