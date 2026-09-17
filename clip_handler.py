"""Robust /clip parser and source resolver.

Keeps the stable bot command layer untouched while accepting multi-word anime
names and common S1/E5 or Season 1 Episode 5 spellings.
"""

import re

from database import get_animes, get_best_source, get_connection
from ffmpeg_utils import get_duration, make_clip, parse_time, format_time
from library_nav import canonical_anime
from telegram_media import download_telethon_message


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

    if requested_canonical:
        for stored in animes:
            if canonical_anime(stored) == requested_canonical:
                return stored

    requested_norm = re.sub(r"[^a-z0-9]+", " ", requested.casefold()).strip()
    for stored in animes:
        stored_norm = re.sub(r"[^a-z0-9]+", " ", stored.casefold()).strip()
        if stored_norm == requested_norm:
            return stored

    for stored in animes:
        if canonical_anime(stored) == requested_canonical:
            return stored

    return None


def _resolve_source(anime: str, season: str, episode: str):
    """Resolve an episode even when legacy sync data used slightly different
    season/episode text representations.
    """
    season = str(int(season))
    episode = str(int(episode))

    source = get_best_source(anime, season, episode)
    if source:
        return source, season, episode

    # Legacy/edge-case fallback: compare numeric season/episode values directly
    # instead of requiring the database text representation to be identical.
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT season, episode, quality, source_url
            FROM library
            WHERE LOWER(anime) = LOWER(?)
            ORDER BY
                CASE quality
                    WHEN '720p' THEN 1
                    WHEN '1080p' THEN 2
                    WHEN '480p' THEN 3
                    WHEN '360p' THEN 4
                    WHEN '1440p' THEN 5
                    WHEN '2160p' THEN 6
                    ELSE 7
                END
            """,
            (anime,),
        ).fetchall()

    for row in rows:
        db_season = str(row["season"]).strip()
        db_episode = str(row["episode"]).strip()
        if db_season.isdigit() and db_episode.isdigit():
            if int(db_season) == int(season) and int(db_episode) == int(episode):
                return row["source_url"], db_season, db_episode

    # Some old Naruto uploads were episode-only and implicitly Season 1.
    if season == "1":
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT season, episode, quality, source_url
                FROM library
                WHERE LOWER(anime) = LOWER(?)
                  AND episode = ?
                ORDER BY
                    CASE quality
                        WHEN '720p' THEN 1
                        WHEN '1080p' THEN 2
                        WHEN '480p' THEN 3
                        WHEN '360p' THEN 4
                        WHEN '1440p' THEN 5
                        WHEN '2160p' THEN 6
                        ELSE 7
                    END
                """,
                (anime, episode),
            ).fetchall()
        if rows:
            row = rows[0]
            return row["source_url"], str(row["season"]), str(row["episode"])

    return None, season, episode


async def clip_command(update, context):
    """Handle both active-video and source-episode /clip forms."""
    import bot as bot_module

    user_id = update.effective_user.id
    args = list(context.args)

    try:
        if len(args) == 3 and args[1] == "-":
            input_path = await bot_module._get_active_video(user_id)
            if input_path is None:
                raise ValueError("Pehle original video bhejo.")
            start = parse_time(args[0])
            end = parse_time(args[2])
        else:
            text = " ".join(args).strip()
            match = _SOURCE_RE.fullmatch(text)
            if not match:
                raise ValueError(
                    "Usage:\n/clip 01:20 - 01:50\n"
                    "or\n/clip Naruto Shippuden S1 E27 01:20 - 01:50"
                )

            requested_anime = match.group("anime").strip()
            season = match.group("season")
            episode = match.group("episode")
            start = parse_time(match.group("start"))
            end = parse_time(match.group("end"))

            anime = _resolve_anime(requested_anime)
            if not anime:
                raise ValueError(
                    f"Anime '{requested_anime}' library me nahi mila.\n"
                    "/library se available anime names check karo."
                )

            source, resolved_season, resolved_episode = _resolve_source(
                anime, season, episode
            )
            if not source:
                raise ValueError(
                    f"{anime} S{season} E{episode} ka source library me nahi mila."
                )

            if bot_module.telethon_client is None:
                raise ValueError("Telegram source client connected nahi hai.")

            input_path = await download_telethon_message(
                bot_module.telethon_client,
                source,
                user_id,
            )

        duration = await get_duration(input_path)
        if start < 0 or end <= start or end > duration:
            raise ValueError(f"Video duration {format_time(duration)} hai.")

        output = await make_clip(input_path, start, end, name="clip")
        await bot_module.send_file(
            update,
            output,
            f"✂️ {format_time(start)} → {format_time(end)}",
        )

    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
