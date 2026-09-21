import io
import json
from datetime import datetime, timezone
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError


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
