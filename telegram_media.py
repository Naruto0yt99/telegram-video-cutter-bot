import json
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

REMOTE_META_SUFFIX = ".telegram_remote.json"


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


def _remote_meta_path(user_id, message_id):
    user_dir = ensure_dir(Path(TEMP_DIR) / str(user_id))
    return user_dir / f"telegram_{message_id}{REMOTE_META_SUFFIX}"


def is_remote_video_path(path):
    return str(path).endswith(REMOTE_META_SUFFIX)


def read_remote_video_meta(path):
    path = Path(path)
    if not is_remote_video_path(path):
        raise ValueError("Ye Telegram remote video metadata nahi hai.")
    if not path.exists():
        raise RuntimeError("Telegram remote video metadata missing hai.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "chat_id" not in data or "message_id" not in data:
        raise RuntimeError("Telegram remote video metadata invalid hai.")
    return data


async def download_bot_video(message, user_id):
    """Register an incoming Telegram video without downloading it.

    The normal Bot API file download is intentionally NOT used here.  A tiny
    metadata file is stored locally and the actual media is later read by the
    Telethon USER_SESSION through Telegram's range API when /clip or /split
    needs a portion of the video.
    """
    if not is_video_message(message):
        raise ValueError("Telegram message me video nahi hai.")

    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        raise RuntimeError("Telegram message reference nahi mila.")

    path = _remote_meta_path(user_id, message_id)
    payload = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "filename": get_message_video_name(message),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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
