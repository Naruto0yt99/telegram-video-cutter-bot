import asyncio
import logging
import shutil
from pathlib import Path

from database import get_seasons, get_episodes, get_best_source
from telegram_media import parse_telegram_message_link
from telegram_remote import open_telegram_range_server, get_telegram_video_info
from ffmpeg_utils import run_command, make_clip, merge_videos
from config import TEMP_DIR, FFMPEG_BIN
from gemini_analyzer import analyze_video, verify_candidate_window

logger = logging.getLogger("find-engine")

REMOTE_HTTP_OPTIONS = [
    "-seekable", "1",
    "-multiple_requests", "1",
    "-initial_request_size", str(2 * 1024 * 1024),
    "-request_size", str(2 * 1024 * 1024),
    "-short_seek_size", str(2 * 1024 * 1024),
]


async def _extract_remote_window(server, output_path, source_start, duration):
    duration = max(1.0, float(duration))
    source_start = max(0.0, float(source_start))
    await run_command(
        FFMPEG_BIN,
        "-y",
        *REMOTE_HTTP_OPTIONS,
        "-ss", str(source_start),
        "-i", server.url,
        "-t", str(duration),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "20",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output_path),
    )


async def _verify_remote_candidate(client, candidate, segment, job_dir, base_time, width):
    server = None
    try:
        server = await open_telegram_range_server(client, candidate["source_url"])
        probe = job_dir / (
            f"candidate_{candidate['season']}_{candidate['episode']}_"
            f"{int(base_time * 1000)}_{int(width)}.mp4"
        )
        await _extract_remote_window(server, probe, base_time, width)
        if not probe.exists() or probe.stat().st_size == 0:
            return None
        verified = await verify_candidate_window(
            probe,
            segment,
            base_time,
            base_time + width,
        )
        if verified.get("match") and float(verified.get("confidence", 0)) >= 0.72:
            return probe, verified
        probe.unlink(missing_ok=True)
        return None
    except Exception as exc:
        logger.info(
            "Remote candidate failed for %s: %s",
            candidate.get("source_url"),
            exc,
        )
        return None
    finally:
        if server is not None:
            await server.close()


async def _try_candidate(client, candidate, segment, user_id, job_dir):
    duration = max(
        0.5,
        float(segment["end_time"]) - float(segment["start_time"]),
    )
    chat, message_id = parse_telegram_message_link(candidate["source_url"])

    try:
        _, source_duration, _ = await get_telegram_video_info(
            client,
            chat,
            message_id,
        )
    except Exception as exc:
        logger.info("Source metadata failed: %s", exc)
        return None

    hint = segment.get("source_start_hint")
    try:
        hint = None if hint is None else float(hint)
    except (TypeError, ValueError):
        hint = None

    if hint is None:
        ratios = [0.05, 0.18, 0.31, 0.44, 0.57, 0.70, 0.83, 0.95]
        width = max(12.0, min(30.0, duration + 10.0))
        for ratio in ratios:
            source_start = max(
                0.0,
                min(max(0.0, source_duration - 0.1), ratio * source_duration),
            )
            result = await _verify_remote_candidate(
                client,
                candidate,
                segment,
                job_dir,
                source_start,
                width,
            )
            if result:
                return result
        return None

    hint = max(0.0, min(hint, max(0.0, source_duration - 0.1)))
    widths = [
        max(12.0, min(30.0, duration + 8.0)),
        max(24.0, min(45.0, duration + 20.0)),
    ]
    for width in widths:
        start = max(0.0, hint - min(4.0, width * 0.2))
        result = await _verify_remote_candidate(
            client,
            candidate,
            segment,
            job_dir,
            start,
            width,
        )
        if result:
            return result
    return None


def candidate_episodes(anime, season, episode):
    if not anime:
        return []
    seasons = [str(season)] if season is not None else get_seasons(anime)
    candidates = []
    for current_season in seasons:
        episodes = (
            [str(episode)]
            if episode is not None
            else get_episodes(anime, current_season)
        )
        for current_episode in episodes:
            source = get_best_source(
                anime,
                current_season,
                current_episode,
            )
            if source:
                candidates.append({
                    "anime": anime,
                    "season": current_season,
                    "episode": current_episode,
                    "source_url": source,
                })
    return candidates


async def _process_segment(client, segment, index, user_id, job_dir):
    anime = segment.get("anime")
    if not anime:
        return None

    candidates = candidate_episodes(
        anime,
        segment.get("season"),
        segment.get("episode"),
    )
    if not candidates:
        return None

    for candidate in candidates:
        result = await _try_candidate(
            client,
            candidate,
            segment,
            user_id,
            job_dir,
        )
        if not result:
            continue

        probe, verified = result
        local_start = max(0.0, float(verified["start_time"]))
        local_end = max(
            local_start + 0.1,
            float(verified["end_time"]),
        )

        clip = await make_clip(
            probe,
            local_start,
            local_end,
            name=f"find_{index:03d}",
        )
        probe.unlink(missing_ok=True)

        return {
            "index": index,
            "clip": clip,
            "confidence": verified.get("confidence", 0),
            "reason": verified.get("reason", ""),
            "candidate": candidate,
        }

    return None


async def find_and_build(
    input_video,
    user_id,
    telethon_client,
    progress_message=None,
):
    if telethon_client is None:
        raise RuntimeError("Telegram source client connected nahi hai.")

    input_video = Path(input_video)
    if not input_video.exists():
        raise RuntimeError("Input video nahi mila.")

    segments = await analyze_video(input_video)
    if not segments:
        raise RuntimeError(
            "Gemini ko koi usable anime segment nahi mila."
        )

    job_dir = Path(TEMP_DIR) / str(user_id) / "find_job"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)

    results = []
    try:
        if progress_message:
            try:
                await progress_message.edit_text(
                    f"🎯 FIND\n\n"
                    f"Gemini analysis complete: {len(segments)} scenes\n"
                    "Targeted Telegram range matching start..."
                )
            except Exception:
                pass

        semaphore = asyncio.Semaphore(3)

        async def worker(index, segment):
            async with semaphore:
                if progress_message:
                    try:
                        await progress_message.edit_text(
                            f"🎯 FIND\n\n"
                            f"Scene {index}/{len(segments)}\n"
                            f"{segment.get('anime') or 'Unknown'} "
                            f"S{segment.get('season') or '?'} "
                            f"E{segment.get('episode') or '?'}\n\n"
                            "Telegram range seek + Gemini verification..."
                        )
                    except Exception:
                        pass
                try:
                    return await _process_segment(
                        telethon_client,
                        segment,
                        index,
                        user_id,
                        job_dir,
                    )
                except Exception as exc:
                    logger.exception(
                        "Scene %s failed: %s",
                        index,
                        exc,
                    )
                    return None

        gathered = await asyncio.gather(
            *(worker(i, segment) for i, segment in enumerate(segments, 1))
        )
        results = [result for result in gathered if result]
        results.sort(key=lambda result: result["index"])

        if not results:
            raise RuntimeError(
                "Koi reliable source clip match nahi mila."
            )

        output = await merge_videos(
            [result["clip"] for result in results],
            "find_result",
        )
        return {
            "output": output,
            "matched": len(results),
            "total": len(segments),
        }
    finally:
        for item in results:
            try:
                item["clip"].unlink(missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(job_dir, ignore_errors=True)
