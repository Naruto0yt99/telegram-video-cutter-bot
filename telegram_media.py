import re
from pathlib import Path

from config import TEMP_DIR
from utils import ensure_dir, safe_filename


TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/"
    r"(?:(?:c/)?[\w+-]+)"
    r"/(\d+)"
    r"(?:\?.*)?$",
    re.IGNORECASE,
)


def is_video_message(message) -> bool:
    if getattr(message, "video", None):
        return True

    document = getattr(message, "document", None)

    if document:
        mime = getattr(document, "mime_type", "") or ""
        return mime.startswith("video/")

    return False


def get_message_video_name(message) -> str:
    document = getattr(message, "document", None)

    if document:
        filename = getattr(document, "file_name", None)
        if filename:
            return safe_filename(filename)

    return "telegram_video.mp4"


async def download_bot_video(message, user_id: int) -> Path:
    if not is_video_message(message):
        raise ValueError("This Telegram message does not contain a video.")

    user_dir = ensure_dir(Path(TEMP_DIR) / str(user_id))

    filename = get_message_video_name(message)

    if not filename.lower().endswith((".mp4", ".mkv", ".mov", ".webm", ".avi")):
        filename += ".mp4"

    destination = user_dir / filename

    await message.download_to_drive(custom_path=str(destination))

    if not destination.exists():
        raise RuntimeError("Telegram video download failed.")

    if destination.stat().st_size == 0:
        raise RuntimeError("Downloaded video is empty.")

    return destination


def parse_telegram_message_link(url: str):
    """
    Returns (chat_reference, message_id).

    Supported:
      https://t.me/channel/123
      https://t.me/c/123456789/123
    """

    url = url.strip()

    match = re.match(
        r"^https?://t\.me/([^/]+)/(\d+)(?:\?.*)?$",
        url,
        re.IGNORECASE,
    )

    if match:
        chat = match.group(1)
        message_id = int(match.group(2))
        return chat, message_id

    match = re.match(
        r"^https?://t\.me/c/(\d+)/(\d+)(?:\?.*)?$",
        url,
        re.IGNORECASE,
    )

    if match:
        internal_id = match.group(1)
        message_id = int(match.group(2))

        # Telethon accepts -100 + internal channel ID.
        chat_id = int("-100" + internal_id)

        return chat_id, message_id

    raise ValueError("Unsupported Telegram message link.")


def is_telegram_message_link(text: str) -> bool:
    try:
        parse_telegram_message_link(text)
        return True
    except ValueError:
        return False


async def download_telethon_message(client, chat, message_id: int, user_id: int) -> Path:
    message = await client.get_messages(chat, ids=message_id)

    if not message:
        raise RuntimeError("Telegram source message was not found.")

    if not message.media:
        raise RuntimeError("The Telegram source message has no media.")

    user_dir = ensure_dir(Path(TEMP_DIR) / str(user_id) / "sources")

    path = await client.download_media(
        message,
        file=str(user_dir) + "/",
    )

    if not path:
        raise RuntimeError("Telethon could not download the source video.")

    result = Path(path)

    if not result.exists() or result.stat().st_size == 0:
        raise RuntimeError("Downloaded Telegram source is empty.")

    return result
