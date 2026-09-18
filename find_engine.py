import logging
import re
from pathlib import Path

from config import FFMPEG_BIN, TEMP_DIR
from database import (
    get_all_sources_for_episode,
    get_all_sources_for_episode_any_season,
)
from ffmpeg_utils import run_command
from gemini_analyzer import analyze_video, verify_source_match
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

    # First use the exact season/episode Gemini returned.
    sources = get_all_sources_for_episode(anime, season, episode)
    resolved_season = season

    # Some anime/source libraries continue episode numbering across seasons,
    # while Gemini may assign the same episode to a different season. If the
    # exact season has no source, safely fall back only when this episode exists
    # in exactly one indexed season. This avoids guessing when episode numbers
    # repeat across multiple seasons.
    if not sources:
        by_season = get_all_sources_for_episode_any_season(anime, episode)
        if len(by_season) == 1:
            resolved_season_text, sources = next(iter(by_season.items()))
            resolved_season = _number(resolved_season_text)
            logger.warning(
                "Scene season mismatch: Gemini=%s S%s E%s; using indexed S%s E%s",
                anime,
                season,
                episode,
                resolved_season,
                episode,
            )
        elif by_season:
            logger.warning(
                "Scene source ambiguous: anime=%r episode=%r exists in seasons=%s; "
                "Gemini requested S%s",
                anime,
                episode,
                sorted(by_season.keys()),
                season,
            )

    if not sources:
        return None, anime, resolved_season, episode, None

    for quality in QUALITY_ORDER:
        if quality in sources:
            return sources[quality], anime, resolved_season, episode, quality

    quality, source = next(iter(sources.items()))
    return source, anime, resolved_season, episode, quality


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


async def _make_edit_sample(input_video, start, duration, output):
    duration = max(1.0, min(float(duration), 12.0))
    await run_command(
        FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
        "-ss", str(max(0.0, float(start))), "-i", str(input_video),
        "-t", str(duration), "-an",
        "-vf", "scale=480:-2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        str(output),
    )
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Edit sample nahi bana.")
    return output


async def _verify_region(input_video, client, source_url, region, output_dir):
    hint = float(region.get("source_start_hint"))
    edit_start = float(region["start_time"])
    edit_length = max(1.0, float(region["end_time"]) - edit_start)
    sample_len = min(12.0, edit_length)
    edit_sample = output_dir / f"verify_edit_{int(edit_start * 1000)}.mp4"
    await _make_edit_sample(input_video, edit_start, sample_len, edit_sample)

    # First pass: a compact +/-60s window around Gemini's hint.
    # Fallback: a wider +/-5min window only when the first pass cannot match.
    windows = [(max(0.0, hint - 60.0), sample_len + 120.0)]
    windows.append((max(0.0, hint - 300.0), sample_len + 600.0))

    best = None
    try:
        for window_start, window_duration in windows:
            source_sample = output_dir / f"verify_source_{int(window_start)}.mp4"
            try:
                await _extract_remote_clip(
                    client, source_url, window_start, window_start + window_duration, source_sample
                )
                result = await asyncio.to_thread(
                    verify_source_match, edit_sample, source_sample, window_start
                )
                if result and result.get("match"):
                    confidence = float(result.get("confidence", 0.0))
                    if best is None or confidence > best[0]:
                        best = (confidence, result, window_start)
                    if confidence >= 0.85:
                        break
            finally:
                source_sample.unlink(missing_ok=True)
    finally:
        edit_sample.unlink(missing_ok=True)

    if not best:
        return None
    _, result, window_start = best
    return max(0.0, window_start + float(result.get("offset_in_source_window", 0.0)))


async def _process_fast_scene(
    input_video,
    telethon_client,
    region,
    index,
    total,
    output_dir,
    progress_message=None,
):
    """Gemini identifies the episode; a source-window Gemini pass refines the timestamp."""
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
    candidate_start = source_start

    try:
        refined = await _verify_region(
            input_video, telethon_client, source, region, output_dir
        )
        if refined is not None:
            candidate_start = refined
    except Exception:
        logger.warning(
            "Scene %s visual verification failed; using Gemini hint",
            index,
            exc_info=True,
        )

    candidate_end = candidate_start + clip_length
    output = unique_path(
        output_dir,
        safe_filename(
            f"find_{index:02d}_{anime}_S{season}E{episode}_{int(candidate_start)}"
        ) + ".mp4",
    )

    if progress_message:
        try:
            await progress_message.edit_text(
                "🎯 FIND\\n\\n"
                f"Scene {index}/{total}\\n"
                f"{anime} S{season} E{episode}\\n"
                f"Source: {candidate_start:.1f}s\\n\\n"
                "🔎 Visual verification + Telegram extraction..."
            )
        except Exception:
            pass

    try:
        await _extract_remote_clip(
            telethon_client, source, candidate_start, candidate_end, output
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
            "offset": candidate_start - source_start,
        }
    except Exception:
        output.unlink(missing_ok=True)
        logger.info("Scene %s extraction failed", index, exc_info=True)
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
                    input_video,
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
