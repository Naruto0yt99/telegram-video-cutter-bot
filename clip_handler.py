"""Simple /clip and /clips source-episode cutter."""

import re

from database import get_animes, get_best_source, get_connection, get_all_sources_for_episode_any_season
from ffmpeg_utils import parse_time, format_time
from library_nav import canonical_anime
from telegram_remote import open_telegram_range_server
from find_engine import _extract_remote_clip
from utils import unique_path


_SOURCE_RE = re.compile(
    r"^(?P<anime>.+?)\s+"
    r"(?:s|season)\s*(?P<season>\d{1,3})\s+"
    r"(?:e|ep|episode)\s*(?P<episode>\d{1,4})\s+"
    r"(?P<start>\d+(?::\d+){1,2}(?:\.\d+)?)\s*-\s*"
    r"(?P<end>\d+(?::\d+){1,2}(?:\.\d+)?)$",
    re.IGNORECASE,
)


def _resolve_anime(requested: str):
    requested = (requested or "").strip()
    requested_canonical = canonical_anime(requested)
    animes = get_animes()
    requested_norm = re.sub(r"[^a-z0-9]+", " ", requested.casefold()).strip()

    for stored in animes:
        if canonical_anime(stored) == requested_canonical:
            return stored
    for stored in animes:
        stored_norm = re.sub(r"[^a-z0-9]+", " ", stored.casefold()).strip()
        if stored_norm == requested_norm:
            return stored
    return None


def _resolve_source(anime: str, season: str, episode: str):
    season = str(int(season))
    episode = str(int(episode))
    source = get_best_source(anime, season, episode)
    if source:
        return source, season, episode

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT season, episode, quality, source_url
            FROM library
            WHERE LOWER(anime) = LOWER(?)
            ORDER BY CASE quality
                WHEN '720p' THEN 1 WHEN '1080p' THEN 2 WHEN '480p' THEN 3
                WHEN '360p' THEN 4 WHEN '1440p' THEN 5 WHEN '2160p' THEN 6 ELSE 7 END
            """,
            (anime,),
        ).fetchall()
    for row in rows:
        if str(row["season"]).isdigit() and str(row["episode"]).isdigit():
            if int(row["season"]) == int(season) and int(row["episode"]) == int(episode):
                return row["source_url"], str(row["season"]), str(row["episode"])

    # Episode numbering is continuous across seasons in this library.
    # If the requested season has no source but this episode exists in exactly
    # one other season, safely resolve to that indexed season instead of failing.
    grouped = get_all_sources_for_episode_any_season(anime, episode)
    if len(grouped) == 1:
        resolved_season = next(iter(grouped))
        sources = grouped[resolved_season]
        for quality in ("720p", "1080p", "480p", "360p", "1440p", "2160p", "auto"):
            if quality in sources:
                return sources[quality], str(resolved_season), episode

    return None, season, episode


async def clip_command(update, context):
    import bot as bot_module

    user_id = update.effective_user.id
    args = list(context.args)
    try:
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
        match = _SOURCE_RE.fullmatch(text)
        if not match:
            raise ValueError(
                "Usage:\n/clip 01:20 - 01:50\n"
                "or\n/clips Naruto Shippuden S1 E27 01:20 - 01:50"
            )

        requested_anime = match.group("anime").strip()
        season = match.group("season")
        episode = match.group("episode")
        start = parse_time(match.group("start"))
        end = parse_time(match.group("end"))

        anime = _resolve_anime(requested_anime)
        if not anime:
            raise ValueError(f"Anime '{requested_anime}' library me nahi mila.")

        source, resolved_season, resolved_episode = _resolve_source(anime, season, episode)
        if not source:
            raise ValueError(f"{anime} S{season} E{episode} ka source library me nahi mila.")
        if bot_module.telethon_client is None:
            raise ValueError("Telegram source client connected nahi hai.")

        output_dir = bot_module.user_temp_dir(user_id)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"clips_{anime}_S{resolved_season}E{resolved_episode}")
        output = unique_path(output_dir, safe + ".mp4")
        await _extract_remote_clip(bot_module.telethon_client, source, start, end, output)
        await bot_module.send_file(
            update,
            output,
            f"✂️ {anime} S{resolved_season} E{resolved_episode} {format_time(start)} - {format_time(end)}",
        )

    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
