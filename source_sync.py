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

_BRACKETED = re.compile(r"[\[\](){}]")
_SEPARATORS = re.compile(r"[|•·]+")


def _clean_caption(text: str) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


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


def _anime_from_caption(text: str, marker):
    if not marker:
        return None

    prefix = text[: marker.start()].strip(" -_.|:•·[](){}")
    prefix = _CONTENT_MARKERS.sub(" ", prefix)
    prefix = _SEPARATORS.sub(" ", prefix)
    prefix = re.sub(r"\s+", " ", prefix).strip(" -_.")

    if not prefix:
        return None

    # Captions often begin with a release/group tag such as [Group].
    # Remove only leading bracketed tags; keep bracketed anime titles intact.
    while True:
        match = re.match(r"^\[[^\]]{1,80}\]\s*", prefix)
        if not match:
            break
        candidate = prefix[match.end():].strip()
        if candidate:
            prefix = candidate
        else:
            break

    prefix = re.sub(r"\s{2,}", " ", prefix).strip(" -_.")
    if len(prefix) < 2:
        return None
    return prefix


def parse_episode_metadata(message: Message):
    filename = get_message_video_name(message)
    caption = _clean_caption(getattr(message, "message", "") or "")
    combined = _clean_caption(f"{caption} {filename}")

    season, episode, marker = _episode_from_text(combined)
    if episode is None:
        return None

    anime = _anime_from_caption(caption, marker)
    if not anime:
        # If the caption itself did not contain the marker, use the filename
        # prefix before the episode marker as a strict fallback.
        anime = _anime_from_caption(combined, marker)

    if not anime:
        return None

    quality = _detect_quality(combined)
    if not quality:
        # Do not guess quality. A source without an explicit quality marker is
        # stored as auto so it can still be used, while never pretending it is
        # 1080p/720p/etc.
        quality = "auto"

    if season is None:
        # A bare Episode N without a season is only safe for season 1 when the
        # caption explicitly says it is season 1.
        if re.search(r"\b(?:season|s)\s*1\b", combined, re.I):
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
    indexed = 0
    skipped = 0
    newest_seen = last_id

    entity = await client.get_entity(SOURCE_CHAT)

    # First run walks the whole history oldest -> newest. Later runs only read
    # messages newer than the checkpoint. Only Telegram metadata is fetched.
    async for message in client.iter_messages(
        entity,
        min_id=last_id if last_id else None,
        reverse=True,
    ):
        if not message or not is_video_message(message):
            if getattr(message, "id", 0) > newest_seen:
                newest_seen = message.id
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

        if message.id > newest_seen:
            newest_seen = message.id

        # Checkpoint regularly so an interrupted initial scan can resume.
        if (indexed + skipped) % 100 == 0:
            _set_sync_state(newest_seen, False)

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

    for anime in animes:
        lines.append(f"🎬 <b>{escape(anime)}</b>")
        for season in get_seasons(anime):
            lines.append(f"  📺 <b>Season {escape(str(season))}</b>")
            for episode in get_episodes(anime, season):
                sources = get_all_sources_for_episode(anime, season, episode)
                if not sources:
                    continue

                quality_links = []
                preferred = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "auto"]
                for quality in preferred:
                    url = sources.get(quality)
                    if url:
                        label = "Source" if quality == "auto" else quality
                        quality_links.append(f'<a href="{escape(url, quote=True)}">{label}</a>')

                episode_label = f"Episode {escape(str(episode))}"
                if quality_links:
                    lines.append(f"    🎞️ <b>{episode_label}</b> — " + " · ".join(quality_links))

        lines.append("")

    return "\n".join(lines).strip()
