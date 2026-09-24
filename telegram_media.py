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
        if mime.startswith("video/"):
            return True

        filename = getattr(document, "file_name", "") or ""
        if filename.lower().endswith(VIDEO_EXTENSIONS):
            return True

    # Telethon Message objects expose uploaded videos as
    # message.media.document, while Bot API Update messages expose them as
    # message.video/message.document. Support both representations because
    # the range proxy reads the source through the Telethon USER_SESSION.
    media = getattr(message, "media", None)
    media_document = getattr(media, "document", None)
    if media_document:
        mime = getattr(media_document, "mime_type", "") or ""
        if mime.startswith("video/"):
            return True

        filename = getattr(media_document, "file_name", "") or ""
        if filename.lower().endswith(VIDEO_EXTENSIONS):
            return True

        for attribute in getattr(media_document, "attributes", []) or []:
            if attribute.__class__.__name__.lower() == "documentattributevideo":
                return True

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

    media = getattr(message, "media", None)
    media_document = getattr(media, "document", None)
    if media_document:
        for attribute in getattr(media_document, "attributes", []) or []:
            filename = getattr(attribute, "file_name", None)
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


async def download_bot_video(message, user_id, mtproto_client=None):
    """Download an incoming Telegram video locally for /clip and /split.

    The old metadata-only implementation required the USER_SESSION to read the
    private bot conversation later. That caused the reported access error because
    the USER_SESSION does not necessarily have access to that chat.
    """
    if not is_video_message(message):
        raise ValueError("Telegram message me video nahi hai.")

    bot = getattr(message, "_bot", None)
    if bot is None:
        raise RuntimeError("Telegram Bot API object unavailable.")

    file_id = None
    video = getattr(message, "video", None)
    if video is not None:
        file_id = getattr(video, "file_id", None)

    if not file_id:
        document = getattr(message, "document", None)
        if document is not None:
            file_id = getattr(document, "file_id", None)

    if not file_id:
        raise RuntimeError("Telegram video ka file_id nahi mila.")

    user_dir = ensure_dir(Path(TEMP_DIR) / str(user_id) / "original")
    filename = safe_filename(get_message_video_name(message))
    if not Path(filename).suffix:
        filename += ".mp4"

    path = Path(user_dir) / filename
    if path.exists():
        path = Path(user_dir) / (
            f"{Path(filename).stem}_{getattr(message, 'message_id', 'video')}"
            f"{Path(filename).suffix}"
        )

    if mtproto_client is not None:
        try:
            mt_message = await mtproto_client.get_messages(message.chat_id, ids=message.message_id)
            if mt_message and mt_message.media:
                downloaded = await mtproto_client.download_media(mt_message, file=str(path))
                if downloaded:
                    path = Path(downloaded)
        except Exception as exc:
            import logging
            logging.getLogger("anime-bot.telegram-media").warning(
                "MTProto bot-media download failed for chat=%s message=%s: %s",
                getattr(message, "chat_id", None),
                getattr(message, "message_id", None),
                exc,
                exc_info=True,
            )

    if not path.exists() or path.stat().st_size == 0:
        telegram_file = await bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=str(path))

    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("Telegram video download empty hai.")

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

    # Telegram message links generated by source_sync may include navigation
    # query strings such as ?single, and users may paste links with fragments.
    # They do not change the chat/message identity, so remove them before
    # matching the canonical Telegram path.
    url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")

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
