import re
from pathlib import Path

from config import TEMP_DIR
from utils import ensure_dir, safe_filename


VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".avi",
)


def is_video_message(message):
    if getattr(message, "video", None):
        return True

    document = getattr(message, "document", None)
    if document:
        mime = getattr(document, "mime_type", "") or ""
        return mime.startswith("video/")

    return False


def get_message_video_name(message):
    document = getattr(message, "document", None)
    if document:
        filename = getattr(document, "file_name", None)
        if filename:
            return safe_filename(filename)

    video = getattr(message, "video", None)
    if video:
        filename = getattr(video, "file_name", None)
        if filename:
            return safe_filename(filename)

    return "telegram_video.mp4"


async def download_bot_video(message, user_id):
    """Download a video received by python-telegram-bot.

    PTB media objects expose a file_id; the Bot API File object is obtained
    through the Bot instance and then downloaded to the local destination.
    """
    if not is_video_message(message):
        raise ValueError("Telegram message me video nahi hai.")

    user_dir = ensure_dir(Path(TEMP_DIR) / str(user_id))
    filename = get_message_video_name(message)

    if not filename.lower().endswith(VIDEO_EXTENSIONS):
        filename += ".mp4"

    destination = user_dir / filename

    media = getattr(message, "video", None) or getattr(message, "document", None)
    if media is None:
        raise ValueError("Telegram message me downloadable video nahi hai.")

    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise ValueError("Telegram video ka file_id nahi mila.")

    bot = message.get_bot()
    telegram_file = await bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=str(destination))

    if not destination.exists():
        raise RuntimeError("Telegram video download failed.")
    if destination.stat().st_size == 0:
        raise RuntimeError("Downloaded video empty hai.")

    return destination


def parse_telegram_message_link(url):
    """
    Supported:

    https://t.me/channel/123
    https://t.me/channel/topic/message
    https://t.me/c/123456789/123
    https://t.me/c/123456789/topic/message
    """
    url = url.strip()

    if not url.startswith(("http://", "https://")):
        raise ValueError("Telegram link must start with https://")

    match = re.match(r"^https?://t\.me/([^/]+)/(\d+)$", url, re.IGNORECASE)
    if match:
        return match.group(1), int(match.group(2))

    match = re.match(r"^https?://t\.me/([^/]+)/(\d+)/(\d+)$", url, re.IGNORECASE)
    if match:
        return match.group(1), int(match.group(3))

    match = re.match(r"^https?://t\.me/c/(\d+)/(\d+)$", url, re.IGNORECASE)
    if match:
        internal_id = match.group(1)
        message_id = int(match.group(2))
        return int("-100" + internal_id), message_id

    match = re.match(r"^https?://t\.me/c/(\d+)/(\d+)/(\d+)$", url, re.IGNORECASE)
    if match:
        internal_id = match.group(1)
        message_id = int(match.group(3))
        return int("-100" + internal_id), message_id

    raise ValueError("Unsupported Telegram message link.")


def is_telegram_message_link(text):
    try:
        parse_telegram_message_link(text)
        return True
    except Exception:
        return False


async def download_telethon_message(client, chat, message_id, user_id):
    message = await client.get_messages(chat, ids=message_id)

    if not message:
        raise RuntimeError("Telegram source message nahi mila.")
    if not message.media:
        raise RuntimeError("Source message me media nahi hai.")

    user_dir = ensure_dir(Path(TEMP_DIR) / str(user_id) / "sources")
    path = await client.download_media(message, file=str(user_dir) + "/")

    if not path:
        raise RuntimeError("Telethon source download failed.")

    result = Path(path)
    if not result.exists() or result.stat().st_size == 0:
        raise RuntimeError("Downloaded source empty hai.")

    return result
