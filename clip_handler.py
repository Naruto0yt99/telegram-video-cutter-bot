"""Flexible library episode resolver and source clip command.

Accepts natural variations such as:
  Naruto S1 E2
  NARUTO SEASON 1 EPISODE 2
  NaRuTo s1 ep5
  NarutO S 1 e4
  Naruto S01E04
  Naruto 1x4
  Naruto season-1 episode-4
and optional time ranges after the episode.
"""

import re
import unicodedata

from database import get_animes, get_connection
from ffmpeg_utils import parse_time, format_time
from library_nav import canonical_anime
from find_engine import _extract_remote_clip
from utils import unique_path


_TIME = r"\d+(?::\d+){1,2}(?:\.\d+)?"

# Deliberately permissive around separators/spaces, while keeping season and
# episode numbers tied to explicit markers so "Naruto 12" is not guessed.
_PATTERNS = (
    re.compile(
        rf"^(?P<anime>.+?)\s*(?:season|s)\s*[-._:]?\s*(?P<season>\d{{1,3}})\s*(?:episode|ep|e)\s*[-._:]?\s*(?P<episode>\d{{1,4}})(?:\s+(?P<start>{_TIME})\s*-\s*(?P<end>{_TIME}))?$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<anime>.+?)\s*(?P<season>\d{{1,3}})\s*[xX×]\s*(?P<episode>\d{{1,4}})(?:\s+(?P<start>{_TIME})\s*-\s*(?P<end>{_TIME}))?$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<anime>.+?)\s*[sS]\s*(?P<season>\d{{1,3}})\s*[eE]\s*(?P<episode>\d{{1,4}})(?:\s+(?P<start>{_TIME})\s*-\s*(?P<end>{_TIME}))?$",
        re.IGNORECASE,
    ),
)


def _normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("–", "-").replace("—", "-").replace("×", "x")
    value = re.sub(r"[|/\\,;_]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _norm(value))


def parse_episode_query(raw: str):
    """Return anime/series + numeric season/episode and optional time range."""
    text = _normalize_query(raw)
    for pattern in _PATTERNS:
        match = pattern.fullmatch(text)
        if not match:
            continue
        data = match.groupdict()
        anime = re.sub(r"\s+", " ", data["anime"]).strip(" -_.:")
        if not anime:
            return None
        return {
            "anime": anime,
            "season": str(int(data["season"])),
            "episode": str(int(data["episode"])),
            "start": data.get("start"),
            "end": data.get("end"),
        }
    return None


def _resolve_anime(requested: str):
    requested = _normalize_query(requested)
    requested_canonical = canonical_anime(requested)
    requested_norm = _norm(requested)
    requested_compact = _compact(requested)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT anime, series FROM library ORDER BY anime COLLATE NOCASE, series COLLATE NOCASE"
        ).fetchall()

    # First match the actual stored series. This is important for the Naruto
    # umbrella where anime="Naruto" and series identifies Shippuden/Movies.
    candidates = []
    for row in rows:
        stored_anime = str(row["anime"])
        stored_series = str(row["series"] or stored_anime)
        for label, value in (("series", stored_series), ("anime", stored_anime)):
            if _norm(value) == requested_norm or _compact(value) == requested_compact:
                candidates.append((stored_anime, stored_series, label))
                break

    if candidates:
        return candidates[0][0], candidates[0][1]

    # Canonical aliases such as "NarutO" -> Naruto and known titles.
    if requested_canonical:
        for row in rows:
            stored_anime = str(row["anime"])
            stored_series = str(row["series"] or stored_anime)
            if canonical_anime(stored_series) == requested_canonical:
                return stored_anime, stored_series
            if canonical_anime(stored_anime) == requested_canonical:
                return stored_anime, stored_series

    # Compact substring match handles harmless punctuation/spacing variants.
    if requested_compact:
        scored = []
        for row in rows:
            stored_anime = str(row["anime"])
            stored_series = str(row["series"] or stored_anime)
            for value in (stored_series, stored_anime):
                compact = _compact(value)
                if requested_compact in compact or compact in requested_compact:
                    scored.append((len(compact), stored_anime, stored_series))
                    break
        if scored:
            scored.sort(key=lambda item: item[0])
            _, stored_anime, stored_series = scored[0]
            return stored_anime, stored_series

    return None, None


def _resolve_source(anime: str, series: str, season: str, episode: str):
    season = str(int(season))
    episode = str(int(episode))

    with get_connection() as conn:
        # Exact series match first.
        row = conn.execute(
            """
            SELECT quality, source_url
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND LOWER(series) = LOWER(?)
              AND season = ?
              AND episode = ?
            ORDER BY CASE quality
                WHEN '2160p' THEN 1 WHEN '1440p' THEN 2 WHEN '1080p' THEN 3
                WHEN '720p' THEN 4 WHEN '480p' THEN 5 WHEN '360p' THEN 6 ELSE 7 END
            LIMIT 1
            """,
            (anime, series, season, episode),
        ).fetchone()
        if row:
            return row["source_url"], season, episode, series

        # If the user only wrote "Naruto", resolve the exact episode across
        # series only when it is unambiguous.
        rows = conn.execute(
            """
            SELECT series, quality, source_url
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
            ORDER BY CASE quality
                WHEN '2160p' THEN 1 WHEN '1440p' THEN 2 WHEN '1080p' THEN 3
                WHEN '720p' THEN 4 WHEN '480p' THEN 5 WHEN '360p' THEN 6 ELSE 7 END
            """,
            (anime, season, episode),
        ).fetchall()

    unique_series = []
    for row in rows:
        value = str(row["series"] or anime)
        if value.casefold() not in {x.casefold() for x in unique_series}:
            unique_series.append(value)

    if len(unique_series) == 1 and rows:
        return rows[0]["source_url"], season, episode, unique_series[0]

    return None, season, episode, series


def _usage():
    return (
        "Example:\n"
        "/clips Naruto S1 E2 01:20 - 01:50\n"
        "/clips NARUTO SEASON 1 EPISODE 2 01:20 - 01:50\n"
        "/clips NaRuTo s1 ep5\n"
        "/clips NarutO S 1 e4\n"
        "/clips Naruto S01E04\n"
        "/clips Naruto 1x4"
    )


async def clip_command(update, context):
    import bot as bot_module

    user_id = update.effective_user.id
    args = list(context.args)

    try:
        # Keep the old active-video /clip syntax intact.
        if len(args) == 3 and args[1] == "-":
            input_path = await bot_module._get_active_video(user_id)
            if input_path is None:
                raise ValueError("Pehle original video bhejo.")
            from ffmpeg_utils import get_duration, make_clip
            start = parse_time(args[0])
            end = parse_time(args[2])
            duration = await get_duration(input_path)
            if start < 0 or end <= start or end > duration:
                raise ValueError(f"Video duration {format_time(duration)} hai.")
            output = await make_clip(input_path, start, end, name="clip")
            await bot_module.send_file(update, output, f"✂️ {format_time(start)} → {format_time(end)}")
            return

        text = " ".join(args).strip()
        parsed = parse_episode_query(text)
        if not parsed:
            raise ValueError(_usage())

        requested_anime = parsed["anime"]
        season = parsed["season"]
        episode = parsed["episode"]

        status = await update.message.reply_text(
            f"🎬 <b>LIBRARY RESOLVE</b>\n🔎 <code>{requested_anime}</code>\n"
            f"📺 S{season} E{episode}\n⏳ Finding exact source...",
            parse_mode="HTML",
        )

        anime, series = _resolve_anime(requested_anime)
        if not anime:
            await bot_module.safe_edit_text(
                status,
                f"❌ <code>{requested_anime}</code> library me nahi mila.\n\n{_usage()}",
                parse_mode="HTML",
            )
            return

        source, resolved_season, resolved_episode, resolved_series = _resolve_source(
            anime, series, season, episode
        )
        if not source:
            await bot_module.safe_edit_text(
                status,
                f"❌ Exact episode source nahi mila: <b>{anime}</b> / <b>{series}</b> / "
                f"S{season} E{episode}.",
                parse_mode="HTML",
            )
            return

        # Episode-only query: return the exact indexed Telegram source link
        # instead of downloading an entire episode onto the phone.
        if not parsed["start"] and not parsed["end"]:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await bot_module.safe_edit_text(
                status,
                f"✅ <b>Exact episode found</b>\n"
                f"🎬 {anime}\n"
                f"📚 {resolved_series}\n"
                f"📺 S{resolved_season} E{resolved_episode}\n\n"
                f"🔗 Source library entry ready.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📺 Open Episode Source", url=source)]]
                ),
            )
            return

        start = parse_time(parsed["start"])
        end = parse_time(parsed["end"])
        if start < 0 or end <= start:
            raise ValueError("Time range invalid hai.")

        await bot_module.safe_edit_text(
            status,
            f"🎬 <b>CLIP</b>\n📚 <b>{anime} — {resolved_series}</b>\n"
            f"📺 S{resolved_season} E{resolved_episode}\n"
            f"🔌 Connecting to Telegram source...",
            parse_mode="HTML",
        )

        from telethon_runtime import ensure_telethon_client
        source_client = await ensure_telethon_client()
        if source_client is None:
            raise ValueError("Telegram source client connected nahi hai.")

        await bot_module.safe_edit_text(
            status,
            f"🎬 <b>CLIP</b>\n📚 <b>{anime} — {resolved_series}</b>\n"
            f"📺 S{resolved_season} E{resolved_episode}\n"
            f"📥 Fetching {format_time(start)} → {format_time(end)}...",
            parse_mode="HTML",
        )

        output_dir = bot_module.user_temp_dir(user_id)
        safe = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            f"clips_{anime}_{resolved_series}_S{resolved_season}E{resolved_episode}",
        )
        output = unique_path(output_dir, safe + ".mp4")
        await _extract_remote_clip(source_client, source, start, end, output)

        await bot_module.safe_edit_text(
            status,
            f"🎬 <b>CLIP</b>\n📚 <b>{anime} — {resolved_series}</b>\n"
            f"📺 S{resolved_season} E{resolved_episode}\n✂️ Clip ready\n📤 Sending...",
            parse_mode="HTML",
        )
        await bot_module.send_file(
            update,
            output,
            f"✂️ {anime} — {resolved_series} S{resolved_season} E{resolved_episode} "
            f"{format_time(start)} - {format_time(end)}",
        )
        await bot_module.safe_edit_text(
            status,
            f"✅ <b>Done</b> — {anime} / {resolved_series} S{resolved_season} E{resolved_episode}\n"
            f"✂️ {format_time(start)} → {format_time(end)}",
            parse_mode="HTML",
        )

    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
