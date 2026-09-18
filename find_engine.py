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
    """Fetch only the requested Telegram byte ranges and cut without re-encoding.

    The source episode is never downloaded in full. FFmpeg asks the local range
    proxy only for bytes needed around the requested timestamp, then remuxes the
    selected packets into a small MP4 that Telegram can upload.
    """
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
            "-initial_request_size", "512K",
            "-request_size", "512K",
            "-short_seek_size", "512K",
            "-ss", str(start),
            "-i", server.url,
            "-t", str(duration),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(output),
        )
    finally:
        await server.close()

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg ne clip output nahi banaya.")
    return output


async def _process_fast_scene(
    telethon_client,
    region,
    index,
    total,
    output_dir,
    progress_message=None,
):
    """Fast/best-effort scene extraction.

    Gemini supplies an approximate source timestamp. We try that point first
    and then nearby offsets up to +/- 5 minutes. The current priority is to
    return a usable clip quickly rather than spend minutes on full verification.
    """
    source, anime, season, episode, quality = _source_for_region(region)
    if not source:
        logger.warning(
            "Scene %s source missing anime=%r season=%r episode=%r",
            index, anime, season, episode,
        )
        return None

    try:
        source_start = float(region.get("source_start_hint"))
    except (TypeError, ValueError):
        return None

    edit_start = float(region["start_time"])
    edit_end = float(region["end_time"])
    clip_length = max(0.5, edit_end - edit_start)

    # Best-effort fallback positions. Accuracy can be improved later.
    offsets = (0, -30, 30, -60, 60, -120, 120, -180, 180, -300, 300)

    for offset in offsets:
        candidate_start = max(0.0, source_start + offset)
        candidate_end = candidate_start + clip_length
        output = unique_path(
            output_dir,
            safe_filename(
                f"find_{index:02d}_{anime}_S{season}E{episode}_{int(candidate_start)}"
            ) + ".mp4",
        )

        if progress_message and offset == 0:
            try:
                await progress_message.edit_text(
                    "🎯 FIND\n\n"
                    f"Scene {index}/{total}\n"
                    f"{anime} S{season} E{episode}\n"
                    f"Gemini approx: {source_start:.1f}s\n\n"
                    "⚡ Fast Telegram clip extraction..."
                )
            except Exception:
                pass

        try:
            await _extract_remote_clip(
                telethon_client,
                source,
                candidate_start,
                candidate_end,
                output,
            )
            return {
                "path": output,
                "index": index,
                "anime": anime,
                "season": season,
                "episode": episode,
                "start": candidate_start,
                "end": candidate_end,
                "quality": quality,
                "offset": offset,
            }
        except Exception:
            output.unlink(missing_ok=True)
            logger.info(
                "Scene %s extraction failed at offset %+ss",
                index,
                offset,
                exc_info=True,
            )

    return None


async def find_and_build(input_video, user_id, telethon_client, progress_message=None):
    """Fast FIND: return best-effort clips instead of waiting for perfect matching."""
    if telethon_client is None:
        raise RuntimeError("Telegram source client connected nahi hai.")

    input_video = Path(input_video)
    if not input_video.exists():
        raise RuntimeError("Input video nahi mila.")

    regions = await analyze_video(input_video)
    if not regions:
        raise RuntimeError("Gemini ko koi usable anime scene nahi mila.")

    logger.info("Fast FIND: Gemini returned %s regions", len(regions))
    output_dir = Path(TEMP_DIR) / str(user_id) / "find_clips"
    output_dir.mkdir(parents=True, exist_ok=True)

    import asyncio
    semaphore = asyncio.Semaphore(3)

    async def worker(index, region):
        async with semaphore:
            try:
                return await _process_fast_scene(
                    telethon_client,
                    region,
                    index,
                    len(regions),
                    output_dir,
                    progress_message,
                )
            except Exception:
                logger.exception("Scene %s failed", index)
                return None

    results = await asyncio.gather(
        *(worker(index, region) for index, region in enumerate(regions, start=1))
    )
    clips = [item for item in results if item]
    clips.sort(key=lambda item: item["index"])

    if not clips:
        raise RuntimeError(
            "Gemini ne scenes diye, lekin Telegram source se koi clip extract nahi ho paya."
        )

    return {
        "clips": clips,
        "matched": len(clips),
        "total": len(regions),
        "regions": regions,
        "output": clips[0]["path"],
    }
