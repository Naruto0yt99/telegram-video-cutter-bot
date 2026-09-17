import logging
import re
from pathlib import Path

from config import FFMPEG_BIN, TEMP_DIR
from database import get_all_sources_for_episode
from ffmpeg_utils import run_command
from gemini_analyzer import analyze_video
from telegram_remote import open_telegram_range_server
from utils import safe_filename, unique_path

logger = logging.getLogger("simple-find")

QUALITY_ORDER = ("720p", "1080p", "480p", "360p", "1440p", "2160p", "auto")


def _number(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group()) if match else None


def _source_for_region(region):
    anime = str(region.get("anime") or "").strip()
    season = _number(region.get("season"))
    episode = _number(region.get("episode"))
    if not anime or season is None or episode is None:
        return None, anime, season, episode, None

    sources = get_all_sources_for_episode(anime, season, episode)
    if not sources:
        return None, anime, season, episode, None

    for quality in QUALITY_ORDER:
        if quality in sources:
            return sources[quality], anime, season, episode, quality

    quality, source = next(iter(sources.items()))
    return source, anime, season, episode, quality


async def _extract_remote_clip(client, source_url, start, end, output):
    server = await open_telegram_range_server(client, source_url)
    try:
        start = max(0.0, float(start))
        duration = max(0.5, float(end) - start)
        await run_command(
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "warning",
            "-y",
            "-seekable", "1",
            "-multiple_requests", "1",
            "-initial_request_size", "2M",
            "-request_size", "2M",
            "-short_seek_size", "2M",
            "-ss", str(start),
            "-i", server.url,
            "-t", str(duration),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output),
        )
    finally:
        await server.close()

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg ne clip output nahi banaya.")
    return output


async def find_and_build(input_video, user_id, telethon_client, progress_message=None):
    """Simple FIND: Gemini identifies approximate episode/timestamps; trust them."""
    if telethon_client is None:
        raise RuntimeError("Telegram source client connected nahi hai.")

    input_video = Path(input_video)
    if not input_video.exists():
        raise RuntimeError("Input video nahi mila.")

    regions = await analyze_video(input_video)
    if not regions:
        raise RuntimeError("Gemini ko koi usable anime scene nahi mila.")

    logger.info("Simple FIND: Gemini returned %s regions", len(regions))
    output_dir = Path(TEMP_DIR) / str(user_id) / "find_clips"
    output_dir.mkdir(parents=True, exist_ok=True)
    clips = []

    for index, region in enumerate(regions, start=1):
        source, anime, season, episode, quality = _source_for_region(region)
        if not source:
            logger.warning(
                "Scene %s source missing anime=%r season=%r episode=%r",
                index, anime, season, episode,
            )
            continue

        source_start = region.get("source_start_hint")
        try:
            source_start = float(source_start)
        except (TypeError, ValueError):
            source_start = None
        if source_start is None:
            logger.warning("Scene %s has no source_start_hint", index)
            continue

        edit_start = float(region["start_time"])
        edit_end = float(region["end_time"])
        clip_length = max(0.5, edit_end - edit_start)
        source_end = source_start + clip_length

        output = unique_path(
            output_dir,
            safe_filename(f"find_{index:02d}_{anime}_S{season}E{episode}") + ".mp4",
        )

        if progress_message:
            try:
                await progress_message.edit_text(
                    "🎯 FIND\n\n"
                    f"Scene {index}/{len(regions)}\n"
                    f"{anime} S{season} E{episode}\n"
                    f"Approx source: {source_start:.1f}s → {source_end:.1f}s\n\n"
                    "✂️ Direct clip extraction..."
                )
            except Exception:
                pass

        try:
            await _extract_remote_clip(
                telethon_client,
                source,
                source_start,
                source_end,
                output,
            )
        except Exception:
            logger.exception("Scene %s direct extraction failed", index)
            continue

        clips.append({
            "path": output,
            "index": index,
            "anime": anime,
            "season": season,
            "episode": episode,
            "start": source_start,
            "end": source_end,
            "quality": quality,
        })

    if not clips:
        raise RuntimeError(
            "Gemini ne scenes diye, lekin matching episode sources library me nahi mile."
        )

    return {
        "clips": clips,
        "matched": len(clips),
        "total": len(regions),
        "regions": regions,
        "output": clips[0]["path"],
    }
