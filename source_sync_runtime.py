import logging

from config import SOURCE_CHAT
from database import add_source, get_connection
from source_sync import _library_has_sources, _message_link, parse_episode_metadata
from telegram_media import is_video_message

logger = logging.getLogger("anime-bot.source-sync")


def _ensure_sync_table():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_sync_state (
                source_chat TEXT PRIMARY KEY,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                initial_complete INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _get_sync_state():
    _ensure_sync_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_message_id, initial_complete FROM source_sync_state WHERE source_chat = ?",
            (str(SOURCE_CHAT),),
        ).fetchone()
    if not row:
        return 0, False
    return int(row[0]), bool(row[1])


def _set_sync_state(last_message_id: int, initial_complete: bool):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO source_sync_state(source_chat, last_message_id, initial_complete, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(source_chat) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                initial_complete = excluded.initial_complete,
                updated_at = excluded.updated_at
            """,
            (str(SOURCE_CHAT), int(last_message_id), int(initial_complete)),
        )
        conn.commit()


async def sync_source_library(client):
    """Compatibility-safe source sync; never passes min_id=None to Telethon."""
    if client is None:
        return {"indexed": 0, "skipped": 0, "last_message_id": 0}

    _ensure_sync_table()
    last_id, initial_complete = _get_sync_state()

    if initial_complete and not _library_has_sources():
        last_id = 0
        initial_complete = False
        _set_sync_state(0, False)

    indexed = 0
    skipped = 0
    newest_seen = last_id

    entity = await client.get_entity(SOURCE_CHAT)
    logger.info(
        "Starting source sync: chat=%s last_message_id=%s initial_complete=%s",
        SOURCE_CHAT,
        last_id,
        initial_complete,
    )

    kwargs = {"entity": entity, "reverse": True}
    if last_id > 0:
        kwargs["min_id"] = last_id

    async for message in client.iter_messages(**kwargs):
        message_id = getattr(message, "id", 0) or 0
        newest_seen = max(newest_seen, message_id)

        if not message or not is_video_message(message):
            skipped += 1
            continue

        metadata = parse_episode_metadata(message)
        link = _message_link(message)
        if not metadata or not link:
            skipped += 1
        else:
            add_source(
                metadata["anime"],
                metadata["season"],
                metadata["episode"],
                metadata["quality"],
                link,
            )
            indexed += 1

        processed = indexed + skipped
        if processed % 100 == 0:
            _set_sync_state(newest_seen, False)
            logger.info(
                "Source sync progress: processed=%s indexed=%s skipped=%s last_message_id=%s",
                processed,
                indexed,
                skipped,
                newest_seen,
            )

    _set_sync_state(newest_seen, True)
    logger.info(
        "Source sync complete: indexed=%s skipped=%s last_message_id=%s initial=%s",
        indexed,
        skipped,
        newest_seen,
        initial_complete,
    )
    return {
        "indexed": indexed,
        "skipped": skipped,
        "last_message_id": newest_seen,
    }
