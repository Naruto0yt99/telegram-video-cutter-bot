import logging
import re
from html import escape

from telethon.tl.types import Message

from config import SOURCE_CHAT
from database import add_source, get_connection
from telegram_media import is_video_message, get_message_video_name
from library_nav import canonical_anime

logger = logging.getLogger("anime-bot.source-sync")
PARSER_VERSION = 26


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
    re.compile(r"\bSeason\s*[:=-]?\s*(?P<season>\d{1,3})\s*[,._-]?\s*(?:⌬\s*)?Episode\s*[:=-]?\s*(?P<episode>\d{1,4})\b", re.I),
    re.compile(r"\bS(?P<season>\d{1,3})\s+(?:EP?|Episode)\s*[-._ ]?(?P<episode>\d{1,4})\b", re.I),
]

_EP_ONLY_PATTERNS = [
    re.compile(r"\b(?:Episode|Ep|E)\s*[-._ ]?(?P<episode>\d{1,4})\b", re.I),
]

_NUMBERED_EPISODE_PATTERN = re.compile(
    r"\b(?P<episode>\d{1,4})\b\s*(?="
    r"$|(?:\[|\]|\(|\)|-|_|\.)|"
    r"(?:\d{3,4}\s*p|4k|uhd|web[- .]?dl|web[- .]?rip|bluray|bdrip|hdr|x264|x265|hevc|avc|aac|10bit|8bit|dual\s*audio|multi\s*audio|hindi|english|japanese|sub(?:bed|s)?|dub(?:bed|s)?)"
    r")",
    re.I,
)

_SPECIAL_PATTERNS = [
    ("movie", re.compile(r"\b(?:Movie|Film)(?:\s*[-:#.]?\s*(?P<episode>\d{1,3}))?\b", re.I)),
    ("ova", re.compile(r"\bOVA(?:\s*[-:#.]?\s*(?P<episode>\d{1,3}))?\b", re.I)),
    ("oad", re.compile(r"\bOAD(?:\s*[-:#.]?\s*(?P<episode>\d{1,3}))?\b", re.I)),
    ("special", re.compile(r"\bSpecial(?:\s*[-:#.]?\s*(?P<episode>\d{1,3}))?\b", re.I)),
]

_CONTENT_MARKERS = re.compile(
    r"\b(?:\d{3,4}p|4k|uhd|web[- .]?dl|web[- .]?rip|bluray|bdrip|hdr|x264|x265|hevc|avc|aac|10bit|8bit|dual\s*audio|multi\s*audio|hindi|english|japanese|sub(?:bed|s)?|dub(?:bed|s)?)\b",
    re.I,
)

_SEPARATORS = re.compile(r"[|•·]+")
_BATCH_LINK_PATTERN = re.compile(r"https?://t\.me/[A-Za-z0-9_+/-]+", re.I)
_BATCH_MARKER_PATTERN = re.compile(
    r"\b(?:batch|saved|save|download|links?|episodes?|season\s*\d+\s*:\s*\d+\s*/\s*\d+)\b",
    re.I,
)


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
            global_match = re.search(
                r"\s*[\[(]\s*(?P<episode>\d{1,4})\s*[\])]",
                text[match.end():],
                re.I,
            )
            if global_match:
                return (
                    match.group("season"),
                    global_match.group("episode"),
                    match,
                    "season",
                    True,
                )
            return match.group("season"), match.group("episode"), match, "season", False

    for content_type, pattern in _SPECIAL_PATTERNS:
        match = pattern.search(text)
        if match:
            episode = match.groupdict().get("episode")
            return content_type, episode, match, content_type, bool(episode)

    global_match = re.search(
        r"\b(?:Episode|Ep)\s*[-._ :]*\d{1,4}\s*[\[(]\s*(?P<episode>\d{1,4})\s*[\])]",
        text,
        re.I,
    )
    if global_match:
        return None, global_match.group("episode"), global_match, "season", True

    for pattern in _EP_ONLY_PATTERNS:
        match = pattern.search(text)
        if match:
            return None, match.group("episode"), match, "season", False

    match = None
    for candidate in _NUMBERED_EPISODE_PATTERN.finditer(text):
        match = candidate
    if match:
        return None, match.group("episode"), match, "season", False

    return None, None, None, None, False


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
    prefix = prefix.strip(" -_.[](){}")
    return prefix if len(prefix) >= 2 else None


def _is_batch_or_link_message(text: str) -> bool:
    links = _BATCH_LINK_PATTERN.findall(text or "")
    if len(links) >= 2:
        return True
    return bool(_BATCH_MARKER_PATTERN.search(text or "")) and len(links) >= 1


def _is_non_anime_topic(topic_text: str) -> bool:
    text = _clean_caption(topic_text).casefold()
    if not text:
        return False
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    blocked = {
        "clip", "clips", "twixter", "normal video", "normal videos",
        "video", "videos", "ai", "ai video", "ai videos", "ai generated",
        "edit", "edits", "amv", "short", "shorts", "meme", "memes",
        "random", "other", "others", "misc", "miscellaneous",
    }
    return text in blocked


def _is_obvious_non_anime_title(text: str) -> bool:
    value = _clean_caption(text).casefold()
    if not value:
        return True
    # Telegram's autogenerated video filenames are never anime titles.
    # Reject them before they can become a generic anime candidate.
    if re.fullmatch(r"video[_ -]?\d{4}[-_]\d{2}[-_]\d{2}(?:[_ -].*)?", value):
        return True
    normalized = re.sub(r"[^a-z0-9]+", " ", value).strip()
    if normalized in {
        "clip", "clips", "twixter", "normal video", "normal videos",
        "video", "videos", "ai", "ai video", "ai videos", "ai generated",
        "edit", "edits", "amv", "short", "shorts", "meme", "memes",
        "random", "other", "others", "misc", "miscellaneous",
    }:
        return True
    if re.fullmatch(r"video\s*\d{4}[-_]\d{2}[-_]\d{2}.*", value):
        return True
    if re.fullmatch(r"🎬?\s*clip\s*\d+\s*/.*", value, re.I):
        return True
    if re.match(r"^🎬?\s*clip\b", value, re.I):
        return True
    return False


def _topic_anime_fallback(topic_text: str):
    text = _clean_caption(topic_text)
    if not text:
        return None
    text = re.sub(r"\b(?:Season|S)\s*[-._ ]?\d{1,3}\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:OVA|OAD|Specials?|Movies?)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -_.:|")
    if not text:
        return None
    known = canonical_anime(text)
    return known or (text if len(text) >= 2 and len(text) <= 120 else None)


def _canonical_from_candidates(*values):
    # Known aliases are canonicalized; otherwise preserve the detected title.
    # This keeps source indexing generic for every anime added to the forum.
    for value in values:
        if not value:
            continue
        canonical = canonical_anime(value)
        if canonical:
            return canonical
    for value in values:
        if value:
            cleaned = _clean_caption(value)
            if cleaned and cleaned.casefold() not in {"chats", "clips"}:
                return cleaned
    return None


def _naruto_global_episode(anime, season, episode):
    if anime != "Naruto Shippuden" or season not in {"16", "17"}:
        return None
    local = int(episode)
    max_local = {"16": 13, "17": 11}[season]
    if not 1 <= local <= max_local:
        return None
    base = {"16": 348, "17": 361}[season]
    return str(base + local)


def parse_episode_metadata(
    message: Message,
    topic_text: str = "",
    context_anime: str | None = None,
    context_season: str | None = None,
):
    filename = _clean_caption(get_message_video_name(message))
    caption = _clean_caption(getattr(message, "message", "") or "")
    combined = _clean_caption(f"{caption} {filename}")

    season, episode, marker, content_type, has_global_episode = _episode_from_text(caption)
    marker_source = caption
    if episode is None:
        season, episode, marker, content_type, has_global_episode = _episode_from_text(combined)
        marker_source = combined

    topic_text = _clean_caption(topic_text)
    # The source forum contains non-anime topics too. They must never enter
    # the anime library, even if their filenames happen to contain numbers.
    if _is_non_anime_topic(topic_text):
        return None
    topic_season = None
    topic_match = re.search(r"\b(?:Season|S)\s*[-._ ]?(\d{1,3})\b", topic_text, re.I)
    if topic_match:
        topic_season = str(int(topic_match.group(1)))

    if (episode is None and content_type == "season") or not marker:
        return None

    if content_type == "season" and season is None and topic_season:
        season = topic_season
    if content_type == "season" and season is None and context_season:
        season = context_season

    anime_from_caption = _anime_from_text(caption, marker) if marker_source == caption else _anime_from_text(marker_source, marker)
    filename_parts = _episode_from_text(filename)
    anime_from_filename = _anime_from_text(filename, filename_parts[2]) if filename_parts[2] else None

    if not anime_from_caption:
        anime_from_caption = canonical_anime(caption)
    # Forum topic/context is authoritative; provider bot names must not become anime titles.
    anime = _canonical_from_candidates(
        context_anime,
        topic_text,
        anime_from_caption,
        anime_from_filename,
        caption,
        filename,
    )
    if not anime or _is_obvious_non_anime_title(anime):
        return None

    quality = _detect_quality(combined) or "auto"

    if content_type == "season":
        if season is None and anime == "Naruto Shippuden":
            global_episode = int(episode)
            if 349 <= global_episode <= 360:
                season = "16"
            elif 361 <= global_episode <= 372:
                season = "17"
        if season is None and re.search(r"\b(?:season|s)\s*1\b", combined, re.I):
            season = "1"
        if season is None:
            return None

        season_value = str(int(season))

        # For S16/S17, the channel sometimes switches from global numbering
        # like "(361)" to local numbering like "Episode - 03". When the
        # season is known from the surrounding batch, normalize the local
        # number back to the global source-library episode id.
        if not has_global_episode:
            normalized = _naruto_global_episode(anime, season_value, episode)
            if normalized:
                episode = normalized
    else:
        season_value = content_type
        # Movies/OVAs/OADs/specials are often uploaded with a title but
        # without an explicit ordinal (e.g. "Movie - The Lost Tower").
        # Keep the title so sync_source_library can assign a stable ordinal
        # shared by all quality variants of the same source.
        if episode is None:
            special_title = _clean_caption(
                f"{caption} {filename}"
            )
            special_title = _CONTENT_MARKERS.sub(" ", special_title)
            special_title = re.sub(r"\s+", " ", special_title).strip(" -_.|:•·[](){}")
        else:
            special_title = None

    library_anime = "Naruto" if anime in {"Naruto", "Naruto Shippuden", "Naruto Movies"} else anime
    library_series = anime
    return {
        "anime": library_anime,
        "series": library_series,
        "season": season_value,
        "episode": str(int(episode)) if episode is not None else None,
        "quality": quality,
        "special_title": special_title,
    }


def _ensure_sync_table():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_sync_state (
                source_chat TEXT PRIMARY KEY,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                initial_complete INTEGER NOT NULL DEFAULT 0,
                parser_version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(source_sync_state)").fetchall()]
        if "parser_version" not in columns:
            conn.execute("ALTER TABLE source_sync_state ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 1")
        conn.commit()


def _get_sync_state():
    _ensure_sync_table()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_message_id, initial_complete, parser_version FROM source_sync_state WHERE source_chat = ?",
            (str(SOURCE_CHAT),),
        ).fetchone()
    if not row:
        return 0, False
    if int(row[2] or 1) != PARSER_VERSION:
        return 0, False
    return int(row[0]), bool(row[1])


def _set_sync_state(last_message_id: int, initial_complete: bool):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO source_sync_state(source_chat, last_message_id, initial_complete, parser_version, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(source_chat) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                initial_complete = excluded.initial_complete,
                parser_version = excluded.parser_version,
                updated_at = excluded.updated_at
            """,
            (str(SOURCE_CHAT), int(last_message_id), int(initial_complete), PARSER_VERSION),
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


async def _topic_text_for_message(client, entity, message, cache):
    reply = getattr(message, "reply_to", None)
    top_id = None
    if reply:
        top_id = getattr(reply, "reply_to_top_id", None)
        if not top_id:
            top_id = getattr(reply, "reply_to_msg_id", None)
    if not top_id:
        return ""
    if top_id in cache:
        return cache[top_id]
    try:
        root = await client.get_messages(entity, ids=top_id)
        text = _clean_caption(getattr(root, "message", "") or "") if root else ""
        if root:
            action = getattr(root, "action", None)
            title = getattr(action, "title", None)
            if title and len(_clean_caption(title)) >= 2:
                text = _clean_caption(title) if not text else f"{_clean_caption(title)} {text}"
    except Exception:
        logger.debug("Could not read topic root id=%s", top_id, exc_info=True)
        text = ""
    cache[top_id] = text
    return text

def _topic_title_from_message(message):
    action = getattr(message, "action", None)
    title = getattr(action, "title", None)
    return _clean_caption(title) if title else ""


def _reset_index_for_parser_upgrade(last_version: int):
    if last_version == PARSER_VERSION:
        return
    chat_name = str(SOURCE_CHAT).lstrip("@")
    prefix = f"https://t.me/{chat_name}/%"
    with get_connection() as conn:
        conn.execute("DELETE FROM library WHERE source_url LIKE ?", (prefix,))
        conn.commit()
    logger.info("Rebuilding source index after parser upgrade %s -> %s", last_version, PARSER_VERSION)


async def sync_source_library(client):
    if client is None:
        return {"indexed": 0, "skipped": 0, "last_message_id": 0}

    _ensure_sync_table()
    last_id, initial_complete = _get_sync_state()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT parser_version FROM source_sync_state WHERE source_chat = ?",
            (str(SOURCE_CHAT),),
        ).fetchone()
    stored_version = int(row[0]) if row and row[0] is not None else 0
    if stored_version != PARSER_VERSION:
        _reset_index_for_parser_upgrade(stored_version)
        last_id = 0
        initial_complete = False
        _set_sync_state(0, False)

    if initial_complete and not _library_has_sources():
        last_id = 0
        initial_complete = False
        _set_sync_state(0, False)

    indexed = 0
    skipped = 0
    newest_seen = last_id
    topic_cache = {}
    context_anime = None
    context_season = None
    special_ordinals = {}

    entity = await client.get_entity(SOURCE_CHAT)
    logger.info(
        "Starting source sync: chat=%s last_message_id=%s initial_complete=%s parser_version=%s",
        SOURCE_CHAT,
        last_id,
        initial_complete,
        PARSER_VERSION,
    )

    iter_kwargs = {"entity": entity, "reverse": True}
    if last_id > 0:
        iter_kwargs["min_id"] = last_id

    async for message in client.iter_messages(**iter_kwargs):
        message_id = getattr(message, "id", 0) or 0
        newest_seen = max(newest_seen, message_id)

        topic_title = _topic_title_from_message(message)
        if topic_title:
            topic_anime = _topic_anime_fallback(topic_title)
            if topic_anime:
                context_anime = topic_anime
                season_match = re.search(r"\b(?:Season|S)\s*[-._ ]?(\d{1,3})\b", topic_title, re.I)
                context_season = str(int(season_match.group(1))) if season_match else None

        if not message or not is_video_message(message):
            skipped += 1
            continue

        topic_text = await _topic_text_for_message(client, entity, message, topic_cache)

        raw_text = _clean_caption(
            f"{getattr(message, 'message', '') or ''} {get_message_video_name(message)}"
        )
        if _is_batch_or_link_message(raw_text):
            skipped += 1
            if message_id <= 100:
                logger.info(
                    "SOURCE DEBUG id=%s skipped=batch/link post caption=%r",
                    message_id,
                    _clean_caption(getattr(message, "message", "") or ""),
                )
            continue

        local_context_anime = context_anime
        local_context_season = context_season
        topic_anime = _topic_anime_fallback(topic_text)
        topic_match = re.search(r"\b(?:Season|S)\s*[-._ ]?(\d{1,3})\b", topic_text, re.I)
        if topic_anime:
            local_context_anime = topic_anime
        if topic_match:
            local_context_season = str(int(topic_match.group(1)))

        metadata = parse_episode_metadata(
            message,
            topic_text,
            context_anime=local_context_anime,
            context_season=local_context_season,
        )
        link = _message_link(message)

        if metadata:
            context_anime = metadata["anime"]
            context_season = metadata["season"]
        elif topic_text:
            topic_anime = canonical_anime(topic_text)
            if topic_anime:
                context_anime = topic_anime
            season_match = re.search(r"\b(?:Season|S)\s*[-._ ]?(\d{1,3})\b", topic_text, re.I)
            if season_match:
                context_season = str(int(season_match.group(1)))

        if message_id <= 100:
            logger.info(
                "SOURCE DEBUG id=%s video_name=%r caption=%r topic=%r context=%r/%r metadata=%r",
                message_id,
                _clean_caption(get_message_video_name(message)),
                _clean_caption(getattr(message, "message", "") or ""),
                topic_text,
                local_context_anime,
                local_context_season,
                metadata,
            )

        if not metadata or not link:
            skipped += 1
        else:
            episode_value = metadata.get("episode")
            special_title = metadata.get("special_title")
            if episode_value is None and special_title:
                anime_key = metadata["anime"]
                season_key = metadata["season"]
                title_key = re.sub(r"\s+", " ", special_title).casefold()
                bucket = special_ordinals.setdefault((anime_key, season_key), {})
                if title_key not in bucket:
                    bucket[title_key] = len(bucket) + 1
                episode_value = str(bucket[title_key])

            if episode_value is None:
                skipped += 1
            else:
                add_source(
                    metadata["anime"],
                    metadata["season"],
                    episode_value,
                    metadata["quality"],
                    link,
                    series=metadata.get("series"),
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
                lines.append(
                    f"    🎞️ <b>Episode {escape(str(episode))}</b> — "
                    + " · ".join(quality_links)
                )
        lines.append("")

    return "\n".join(lines).strip()
