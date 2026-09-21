import io
import json
from datetime import datetime, timezone

from telegram import Bot
from telegram.error import TelegramError


async def check_fingerprint_storage(bot: Bot, chat_id: str):
    """Verify that this bot can access and post to the fingerprint channel."""
    chat = await bot.get_chat(chat_id)
    me = await bot.get_me()
    member = await bot.get_chat_member(chat.id, me.id)

    status = getattr(member, "status", None)
    can_post = bool(getattr(member, "can_post_messages", False))

    return {
        "chat_id": chat.id,
        "chat_title": getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id),
        "chat_username": getattr(chat, "username", None),
        "bot_id": me.id,
        "bot_username": me.username,
        "status": status,
        "can_post_messages": can_post,
        "ok": status in {"administrator", "creator"} and can_post,
    }


async def save_fingerprint_json(bot: Bot, chat_id: str, fingerprint: dict, filename: str):
    """Upload one compact fingerprint JSON artifact to Telegram."""
    check = await check_fingerprint_storage(bot, chat_id)
    if not check["ok"]:
        raise PermissionError(
            f"Fingerprint storage unavailable: status={check['status']}, "
            f"can_post_messages={check['can_post_messages']}"
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
        f"🕒 {datetime.now(timezone.utc).isoformat()}"
    )

    return await bot.send_document(
        chat_id=chat_id,
        document=bio,
        caption=caption,
    )


async def fingerprint_storage_status(bot: Bot, chat_id: str) -> str:
    try:
        info = await check_fingerprint_storage(bot, chat_id)
    except TelegramError as exc:
        return (
            "❌ FINGERPRINT STORAGE\n\n"
            f"Channel: {chat_id}\n"
            f"Telegram error: {exc}"
        )

    if info["ok"]:
        return (
            "✅ FINGERPRINT STORAGE READY\n\n"
            f"📦 Channel: {info['chat_title']}\n"
            f"🔗 @{info['chat_username'] or 'private'}\n"
            f"🤖 @{info['bot_username'] or info['bot_id']}\n"
            "👑 Status: administrator\n"
            "📝 can_post_messages: TRUE\n\n"
            "🧠 Fingerprint JSON files can now be uploaded here."
        )

    return (
        "⚠️ FINGERPRINT STORAGE NOT READY\n\n"
        f"📦 Channel: {info['chat_title']}\n"
        f"🤖 @{info['bot_username'] or info['bot_id']}\n"
        f"👤 Status: {info['status']}\n"
        f"📝 can_post_messages: {info['can_post_messages']}\n\n"
        "Bot ko channel admin bana kar 'Post Messages' permission ON karo."
    )
