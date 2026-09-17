import asyncio
import logging
import shutil
from pathlib import Path

from database import get_seasons, get_episodes, get_best_source
from telegram_media import parse_telegram_message_link
from telegram_remote import open_telegram_range_server, get_telegram_video_info
from ffmpeg_utils import run_command, make_clip_exact, merge_videos
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


async def _make_target_segment(input_video, segment, job_dir, index):
    start = max(0.0, float(segment["start_time"]))
    duration = max(0.2, float(segment["end_time"]) - start)
    target = job_dir / f"target_{index:03d}.mp4"
    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss", str(start),
        "-i", str(input_video),
        "-t", str(duration),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "20",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(target),
    )
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("Target edit scene extraction failed.")
    return target


async def _verify_remote_candidate(
    client,
    candidate,
    segment,
    target_video,
    job_dir,
    base_time,
    width,
):
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
            target_video_path=target_video,
        )
        confidence = float(verified.get("confidence", 0) or 0)
        if verified.get("match") and confidence >= 0.68:
            return probe, verified

        probe.unlink(missing_ok=True)
        return None
    except Exception as exc:
        logger.info(
            "Remote candidate failed for %s at %.2fs: %s",
            candidate.get("source_url"),
            base_time,
            exc,
        )
        return None
    finally:
        if server is not None:
            await server.close()


async def _try_candidate(
    client,
    candidate,
    segment,
    target_video,
    user_id,
    job_dir,
):
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

    # Gemini's hint is intentionally treated as approximate.  Episode files can
    # contain different intros/recaps, so a correct scene may be shifted by a
    # sizeable amount from the nominal edit/original timestamp.
    if hint is not None:
        hint = max(0.0, min(hint, max(0.0, source_duration - 0.1)))
        widths = [
            max(16.0, min(36.0, duration + 10.0)),
            max(28.0, min(55.0, duration + 24.0)),
        ]
        offsets = [0.0, -12.0, 12.0, -30.0, 30.0, -60.0, 60.0]
        seen = set()
        for width in widths:
            for offset in offsets:
                start = max(0.0, min(hint + offset, max(0.0, source_duration - 0.1)))
                key = (round(start, 1), round(width, 1))
                if key in seen:
                    continue
                seen.add(key)
                result = await _verify_remote_candidate(
                    client,
                    candidate,
                    segment,
                    target_video,
                    job_dir,
                    start,
                    min(width, max(1.0, source_duration - start)),
                )
                if result:
                    return result

    # No hint, or the hint was not enough.  Use a denser coarse search than the
    # old 8-point sampler.  This still downloads only small remote windows and
    # never stores the full episode.  Search in batches so several windows can
    # be checked concurrently without overwhelming Telegram/Gemini.
    width = max(16.0, min(32.0, duration + 10.0))
    if source_duration <= width:
        starts = [0.0]
    else:
        # Roughly every 5% of the episode, with overlap between windows.
        count = max(12, min(24, int(source_duration / max(1.0, width * 0.55))))
        max_start = max(0.0, source_duration - width)
        starts = [max_start * i / (count - 1) for i in range(count)]

    async def check(start):
        return await _verify_remote_candidate(
            client,
            candidate,
            segment,
            target_video,
            job_dir,
            start,
            min(width, max(1.0, source_duration - start)),
        )

    batch_size = 3
    for batch_start in range(0, len(starts), batch_size):
        batch = starts[batch_start:batch_start + batch_size]
        results = await asyncio.gather(*(check(start) for start in batch), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.info("Coarse candidate check failed: %s", result)
                continue
            if result:
                return result

    return None


def candidate_episodes(anime, season, episode):
    if not anime:
        return []

    # Prefer the exact episode Gemini identified.  If Gemini is uncertain,
    # gracefully widen to the known season/all seasons instead of immediately
    # giving up.  This is important for edits where episode numbering is hidden
    # by crops, overlays or transitions.
    if season is not None:
        seasons = [str(season)]
    else:
        seasons = get_seasons(anime)

    candidates = []
    seen = set()
    for current_season in seasons:
        if episode is not None:
            episodes = [str(episode)]
        else:
            episodes = get_episodes(anime, current_season)

        for current_episode in episodes:
            key = (str(anime).lower(), str(current_season), str(current_episode))
            if key in seen:
                continue
            seen.add(key)
            source = get_best_source(anime, current_season, current_episode)
            if source:
                candidates.append({
                    "anime": anime,
                    "season": current_season,
                    "episode": current_episode,
                    "source_url": source,
                })

    logger.info(
        "Candidate episodes for %s S%s E%s: %s",
        anime,
        season if season is not None else "?",
        episode if episode is not None else "?",
        len(candidates),
    )
    return candidates


async def _process_segment(client, segment, index, user_id, job_dir, input_video):
    anime = segment.get("anime")
    if not anime:
        logger.info("Scene %s skipped: Gemini did not identify an anime", index)
        return None

    target_video = await _make_target_segment(
        input_video,
        segment,
        job_dir,
        index,
    )

    candidates = candidate_episodes(
        anime,
        segment.get("season"),
        segment.get("episode"),
    )
    if not candidates:
        target_video.unlink(missing_ok=True)
        return None

    try:
        for candidate in candidates:
            result = await _try_candidate(
                client,
                candidate,
                segment,
                target_video,
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
            clip = await make_clip_exact(
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
    finally:
        target_video.unlink(missing_ok=True)

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

    logger.info("Gemini segments: %s", segments)

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
                            f"🎯 FIND\n\nScene {index}/{len(segments)}\n"
                            f"{segment.get('anime') or 'Unknown'} "
                            f"S{segment.get('season') or '?'} E{segment.get('episode') or '?'}\n\n"
                            "Telegram range seek + visual Gemini verification..."
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
                        input_video,
                    )
                except Exception as exc:
                    logger.exception("Scene %s failed: %s", index, exc)
                    return None

        gathered = await asyncio.gather(
            *(worker(i, segment) for i, segment in enumerate(segments, 1))
        )
        results = [result for result in gathered if result]
        results.sort(key=lambda result: result["index"])

        if not results:
            raise RuntimeError("Koi reliable source clip match nahi mila.")

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
