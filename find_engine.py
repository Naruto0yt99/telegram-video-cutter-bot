import asyncio
import logging
import shutil
from pathlib import Path

from database import get_seasons, get_episodes, get_best_source
from telegram_media import parse_telegram_message_link
from telegram_remote import download_sparse_window
from ffmpeg_utils import run_command, make_clip, merge_videos
from config import TEMP_DIR, FFMPEG_BIN
from gemini_analyzer import analyze_video, verify_candidate_window

logger = logging.getLogger("find-engine")


async def _candidate_window(client, candidate, user_id, source_hint, width):
    chat, message_id = parse_telegram_message_link(candidate["source_url"])
    return await download_sparse_window(
        client,
        chat,
        message_id,
        user_id,
        max(0.0, source_hint),
        window_seconds=width,
    )


async def _probe_window(window_path, output_path, duration, start=0.0):
    """Materialize a playable local excerpt from a sparse Telegram window."""
    try:
        await run_command(
            FFMPEG_BIN,
            "-y",
            "-ss", str(max(0.0, start)),
            "-i", str(window_path),
            "-t", str(max(1.0, duration)),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output_path),
        )
    except Exception:
        # Re-encode fallback makes partially indexed candidate media easier to decode.
        await run_command(
            FFMPEG_BIN,
            "-y",
            "-ss", str(max(0.0, start)),
            "-i", str(window_path),
            "-t", str(max(1.0, duration)),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output_path),
        )


async def _try_candidate(client, candidate, segment, user_id, job_dir):
    duration = max(0.5, float(segment["end_time"]) - float(segment["start_time"]))
    hint = segment.get("source_start_hint")
    if hint is None:
        # Without a reliable source timestamp we use a small set of proportional probes.
        # This is intentionally bounded; it never downloads the full episode.
        probe_points = [0.12, 0.32, 0.52, 0.72, 0.88]
        for ratio in probe_points:
            try:
                meta = await _candidate_window(
                    client, candidate, user_id, ratio * 1000.0, max(12.0, duration + 8.0)
                )
                probe = job_dir / f"probe_{candidate['episode']}_{ratio:.2f}.mp4"
                await _probe_window(meta["path"], probe, max(12.0, duration + 8.0))
                if probe.exists() and probe.stat().st_size:
                    verified = await verify_candidate_window(
                        probe, segment, ratio * meta["duration"], ratio * meta["duration"] + duration + 8.0
                    )
                    if verified.get("match") and verified.get("confidence", 0) >= 0.72:
                        return meta, probe, verified
            except Exception as exc:
                logger.info("Probe failed for %s: %s", candidate["source_url"], exc)
        return None

    hint = max(0.0, float(hint))
    widths = [max(12.0, duration + 8.0), max(24.0, duration + 16.0)]
    for width in widths:
        try:
            meta = await _candidate_window(
                client, candidate, user_id, hint, width
            )
            probe = job_dir / f"candidate_{candidate['episode']}_{int(hint)}_{int(width)}.mp4"
            await _probe_window(meta["path"], probe, width)
            if not probe.exists() or probe.stat().st_size == 0:
                continue
            verified = await verify_candidate_window(
                probe, segment, hint, hint + width
            )
            if verified.get("match") and verified.get("confidence", 0) >= 0.72:
                return meta, probe, verified
        except Exception as exc:
            logger.info("Candidate failed: %s", exc)
    return None


def candidate_episodes(anime, season, episode):
    if not anime:
        return []
    seasons = [str(season)] if season is not None else get_seasons(anime)
    candidates = []
    for current_season in seasons:
        episodes = [str(episode)] if episode is not None else get_episodes(anime, current_season)
        for current_episode in episodes:
            source = get_best_source(anime, current_season, current_episode)
            if source:
                candidates.append({
                    "anime": anime,
                    "season": current_season,
                    "episode": current_episode,
                    "source_url": source,
                })
    return candidates


async def _process_segment(client, segment, index, user_id, job_dir, total):
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

    # When Gemini knows the episode, only that episode is probed. Unknown episode
    # remains bounded to the indexed library instead of downloading all media.
    for candidate in candidates:
        result = await _try_candidate(
            client, candidate, segment, user_id, job_dir
        )
        if not result:
            continue
        meta, probe, verified = result
        local_start = max(0.0, float(verified["start_time"]))
        local_end = max(local_start + 0.1, float(verified["end_time"]))
        clip = await make_clip(
            probe,
            local_start,
            local_end,
            name=f"find_{index:03d}",
        )
        try:
            meta["path"].unlink(missing_ok=True)
        except Exception:
            pass
        return {
            "index": index,
            "clip": clip,
            "confidence": verified.get("confidence", 0),
            "reason": verified.get("reason", ""),
            "candidate": candidate,
        }
    return None


async def find_and_build(input_video, user_id, telethon_client, progress_message=None):
    if telethon_client is None:
        raise RuntimeError("Telegram source client connected nahi hai.")

    input_video = Path(input_video)
    if not input_video.exists():
        raise RuntimeError("Input video nahi mila.")

    segments = await analyze_video(input_video)
    if not segments:
        raise RuntimeError("Gemini ko koi usable anime segment nahi mila.")

    job_dir = Path(TEMP_DIR) / str(user_id) / "find_job"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)

    results = []
    try:
        if progress_message:
            try:
                await progress_message.edit_text(
                    f"🎯 FIND\n\nGemini analysis complete: {len(segments)} scenes\n"
                    "Targeted Telegram matching start..."
                )
            except Exception:
                pass

        # Run independent scenes concurrently, but Telegram/Gemini pressure is kept
        # bounded so one bad source does not block the whole job.
        semaphore = asyncio.Semaphore(3)

        async def worker(index, segment):
            async with semaphore:
                if progress_message:
                    try:
                        await progress_message.edit_text(
                            f"🎯 FIND\n\nScene {index}/{len(segments)}\n"
                            f"{segment.get('anime') or 'Unknown'} "
                            f"S{segment.get('season') or '?'} E{segment.get('episode') or '?'}\n\n"
                            "Targeted source window + Gemini verification..."
                        )
                    except Exception:
                        pass
                try:
                    return await _process_segment(
                        telethon_client, segment, index, user_id, job_dir, len(segments)
                    )
                except Exception as exc:
                    logger.exception("Scene %s failed: %s", index, exc)
                    return None

        results = await asyncio.gather(
            *(worker(i, segment) for i, segment in enumerate(segments, 1))
        )
        results = [r for r in results if r]
        results.sort(key=lambda r: r["index"])

        if not results:
            raise RuntimeError("Koi reliable source clip match nahi mila.")

        clips = [r["clip"] for r in results]
        output = await merge_videos(clips, "find_result")
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
