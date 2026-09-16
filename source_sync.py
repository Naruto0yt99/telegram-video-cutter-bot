import logging
import re
from html import escape

from telethon.tl.types import Message

from config import SOURCE_CHAT
from database import add_source, get_connection
from telegram_media import is_video_message, get_message_video_name

logger = logging.getLogger("anime-bot.source-sync")


_QUALITY_PATTERNS = [
    ("2160p", re.compile(r"(?<!\d)(?:2160p|2160\s*p|4k|uhd)(?!\d)", re.I)),
    ("1440p", re.compile(r"(?<!\d)1440\s*p(?!\d)", re.I)),
    ("1080p", re.compile(r"(?<!\d)1080\s*p(?!\d)", re.I)),
    ("720p", re.compile(r"(?<!\d)720\s*p(?!\d)", re.I)),
    ("480p", re.compile(r"(?<!\d)480\s*p(?!\d)", re.I)),
    ("360p", re.compile(r"(?<!\d)360\s*p(?!\d)", re.I)),
]

_EPISODE_PATTERNS = [
    re.compile(r"\bS(?P<season>\d{1,3})\s*[._-]?\s*E(?P<episode>\d{1,4})\b", re.I),
    re.compile(r"\bSeason\s*(?P<season>\d{1,3})\s*[,._-]?\s*Episode\s*(?P<episode>\d{1,4})\b", re.I),
    re.compile(r"\bS(?P<season>\d{1,3})\s+(?:EP?|Episode)\s*[-._ ]?(?P<episode>\d{1,4})\b", re.I),
]

_EP_ONLY_PATTERNS = [
    re.compile(r"\b(?:Episode|Ep|E)\s*[-._ ]?(?P<episode>\d{1,4})\b", re.I),
]

_CONTENT_MARKERS = re.compile(
    r"\b(?:\d{3,4}p|4k|uhd|web[- .]?dl|web[- .]?rip|bluray|bdrip|hdr|x264|x265|hevc|avc|aac|10bit|8bit|dual\s*audio|multi\s*audio|hindi|english|japanese|sub(?:bed|s)?|dub(?:bed|s)?)\b",
    re.I,
)

_SEPARATORS = re.compile(r"[|•·]+")


def _clean_caption(text: str) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ")
    return re.sub(r"\s+", " ", text).strip()


def _detect_quality(text: str):
    for quality, pattern in _QUALITY_PATTERNS:
        if pattern.search(text):
            return quality
    return None


def _episode_from_text(text: str):
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("season"), match.group("episode"), match
    for pattern in _EP_ONLY_PATTERNS:
        match = pattern.search(text)
        if match:
            return None, match.group("episode"), match
    return None, None, None


def _anime_from_text(text: str, marker):
    if not marker:
        return None
    prefix = text[: marker.start()].strip(" -_.|:•·[](){}")
    prefix = _CONTENT_MARKERS.sub(" ", prefix)
    prefix = _SEPARATORS.sub(" ", prefix)
    prefix = re.sub(r"\s+", " ", prefix).strip(" -_.")
    if not prefix:
        return None
    if prefix.startswith("[") and prefix.endswith("]"):
        inner = prefix[1:-1].strip()
        if len(inner) >= 2:
            prefix = inner
    prefix = re.sub(r"\s{2,}", " ", prefix).strip(" -_.")
    return prefix if len(prefix) >= 2 else None


def parse_episode_metadata(message: Message):
    filename = get_message_video_name(message)
    caption = _clean_caption(getattr(message, "message", "") or "")

    season, episode, marker = _episode_from_text(caption)
    source_text = caption
    if episode is None:
        source_text = _clean_caption(f"{caption} {filename}")
        season, episode, marker = _episode_from_text(source_text)

    if episode is None or not marker:
        return None

    anime = _anime_from_text(source_text, marker)
    if not anime:
        return None

    quality = _detect_quality(source_text) or _detect_quality(filename) or "auto"

    if season is None:
        if re.search(r"\b(?:season|s)\s*1\b", source_text, re.I):
            season = "1"
        else:
            return None

    return {
        "anime": anime,
        "season": str(int(season)),
        "episode": str(int(episode)),
        "quality": quality,
    }


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


def _library_has_sources():
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM library LIMIT 1").fetchone()
    return row is not None


def _message_link(message: Message):
    link = getattr(message, "link", None)
    if link:
        return link
    chat = getattr(message, "chat", None)
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"
    return None


async def sync_source_library(client):
    """Index episode metadata from SOURCE_CHAT without downloading media."""
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

    iter_kwargs = {"entity": entity, "reverse": True}
    if last_id > 0:
        iter_kwargs["min_id"] = last_id

    async for message in client.iter_messages(**iter_kwargs):
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


def render_library_html(animes, get_seasons, get_episodes, get_all_sources_for_episode):
    lines = ["📚 <b>ANIME LIBRARY</b>", ""]
    preferred = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "auto"]

    for anime in animes:
        lines.append(f"🎬 <b>{escape(anime)}</b>")
        for season in get_seasons(anime):
            lines.append(f"  📺 <b>Season {escape(str(season))}</b>")
            for episode in get_episodes(anime, season):
                sources = get_all_sources_for_episode(anime, season, episode)
                if not sources:
                    continue

                best_quality = next((q for q in preferred if q in sources), None)
                if not best_quality:
                    continue

                best_url = sources[best_quality]
                episode_link = (
                    f'<a href="{escape(best_url, quote=True)}">'
                    f'Episode {escape(str(episode))}</a>'
                )

                quality_links = []
                for quality in preferred:
                    url = sources.get(quality)
                    if url:
                        label = "Source" if quality == "auto" else quality
                        quality_links.append(
                            f'<a href="{escape(url, quote=True)}">{label}</a>'
                        )

                lines.append(
                    f"    🎞️ <b>{episode_link}</b> — "
                    + " · ".join(quality_links)
                )

        lines.append("")

    return "\n".join(lines).strip()
