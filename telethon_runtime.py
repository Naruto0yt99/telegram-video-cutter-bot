import asyncio
import sys

from telethon import TelegramClient

from config import TG_API_ID, TG_API_HASH, TELEGRAM_SESSION


_lock = asyncio.Lock()


def _bot_module():
    """Return the already-running bot module without importing a second copy."""
    module = sys.modules.get("__main__")
    if module is not None and hasattr(module, "telethon_client"):
        return module

    module = sys.modules.get("bot")
    if module is not None and hasattr(module, "telethon_client"):
        return module

    raise RuntimeError("Running bot module could not be resolved.")


async def ensure_telethon_client():
    """Return the shared USER_SESSION client, reconnecting it when needed."""
    bot_module = _bot_module()

    client = bot_module.telethon_client
    if client is not None and client.is_connected():
        return client

    async with _lock:
        bot_module = _bot_module()
        client = bot_module.telethon_client
        if client is not None:
            if not client.is_connected():
                await client.connect()
            if await client.is_user_authorized():
                return client

        if not TG_API_ID or not TG_API_HASH:
            raise RuntimeError(
                "Telegram USER_SESSION unavailable: TG_API_ID/TG_API_HASH missing."
            )

        client = TelegramClient(
            TELEGRAM_SESSION,
            TG_API_ID,
            TG_API_HASH,
        )
        await client.start()
        bot_module.telethon_client = client
        return client
