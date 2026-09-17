import asyncio
import logging
import shutil
from pathlib import Path

from database import get_seasons, get_episodes, get_all_sources_for_episode
from telegram_media import parse_telegram_message_link
from telegram_remote import open_telegram_range_server, get_telegram_video_info
from ffmpeg_utils import run_command, make_clip_exact, merge_videos
from config import TEMP_DIR, FFMPEG_BIN
from gemini_analyzer import analyze_video, verify_candidate_window
from visual_matcher import find_visual_match

logger = logging.getLogger("find-engine")

REMOTE_HTTP_OPTIONS = [
    "-seekable", "1",
    "-multiple_requests", "1",
    "-initial_request_size", str(4 * 1024 * 1024),
    "-request_size", str(4 * 1024 * 1024),
    "-short_seek_size", str(4 * 1024 * 1024),
]

MATCH_QUALITY_ORDER = ("720p", "1080p", "480p", "360p", "1440p", "2160p", "auto")


def _candidate_quality_sources(anime, season, episode):
    sources = get_all_sources_for_episode(anime, season, episode)
    if not sources:
        return []
    return [
        {
            "anime": anime,
            "season": str(season),
            "episode": str(episode),
            "quality": q,
            "source_url": sources[q],
        }
        for q in MATCH_QUALITY_ORDER
        if q in sources
    ]


def _landmark_episode_priority(anime, segment):
    name = str(anime or "").lower().replace("-", " ")
    if "naruto" not in name or "shippuden" in name:
        return []
    text = " ".join([
        str(segment.get("arc") or ""),
        " ".join(segment.get("landmarks") or []),
    ]).lower()
    forest = "forest of death" in text or "forest" in text
    anko = "anko" in text or "mitarashi" in text
    entry = any(token in text for token in (
        "before entering", "entering the forest", "forest gate", "forest gates",
        "second exam", "second stage", "scroll",
    ))
    if forest and anko and entry:
        return ["27"]
    return []


def _episode_sort_key(value):
    try:
        return int(str(value))
    except Exception:
        return 10**9


def candidate_episodes(anime, season, episode, segment=None):
    if not anime:
        return []

    seasons = get_seasons(anime)
    requested_season = str(season) if season is not None else None
    requested_episode = str(episode) if episode is not None else None
    priority_episodes = _landmark_episode_priority(anime, segment or {})

    # When Gemini gives a concrete episode, do not scan the entire anime first.
    # Search the requested episode, then a very small same-season neighborhood.
    # This is the main protection against multi-hour remote scans.
    if requested_season in seasons and requested_episode:
        available = get_episodes(anime, requested_season)
        ordered = []
        if requested_episode in available:
            ordered.append(requested_episode)
        numbers = sorted(available, key=_episode_sort_key)
        try:
            pos = numbers.index(requested_episode)
            ordered.extend(numbers[max(0, pos - 2):pos + 3])
        except ValueError:
            pass
        result = []
        seen = set()
        for ep in ordered:
            if ep in seen:
                continue
            seen.add(ep)
            result.extend(_candidate_quality_sources(anime, requested_season, ep))
        return result

    # If the episode is unknown, use strong landmark priorities first and then
    # scan the catalog in bounded batches. The caller stops as soon as a reliable
    # match is found, so it no longer repeats every quality of every episode.
    candidates = []
    seen = set()
    for current_season in seasons:
        available = get_episodes(anime, current_season)
        ordered = []
        for ep in priority_episodes:
            if ep in available:
                ordered.append(ep)
        ordered.extend(sorted(available, key=_episode_sort_key))
        for ep in ordered:
            key = (str(current_season), str(ep))
            if key in seen:
                continue
            seen.add(key)
            candidates.extend(_candidate_quality_sources(anime, current_season, ep))
    return candidates


async def _extract_remote_window(server, output_path, source_start, duration):
    duration = max(1.0, float(duration))
    source_start = max(0.0, float(source_start))
    await run_command(
        FFMPEG_BIN, "-y", *REMOTE_HTTP_OPTIONS,
        "-ss", str(source_start), "-i", server.url, "-t", str(duration),
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-c:a", "aac", "-movflags", "+faststart", str(output_path),
    )


async def _materialize_match(client, candidate, match, segment, job_dir, index):
    server = None
    try:
        server = await open_telegram_range_server(client, candidate["source_url"])
        source_start = max(0.0, float(match["start"]))
        source_end = max(source_start + 0.5, float(match["end"]))
        probe = job_dir / (
            f"match_{index:03d}_{int(source_start * 1000)}_"
            f"{candidate['season']}_{candidate['episode']}.mp4"
        )
        await _extract_remote_window(server, probe, source_start, source_end - source_start)
        if not probe.exists() or probe.stat().st_size == 0:
            return None

        target_duration = max(0.2, float(segment["end_time"]) - float(segment["start_time"]))
        center = float(match.get("center", source_start + (source_end - source_start) / 2.0))
        local_center = max(0.0, center - source_start)
        local_start = max(0.0, local_center - target_duration / 2.0)
        local_end = min(source_end - source_start, local_start + target_duration)
        if local_end - local_start < 0.2:
            return None
        clip = await make_clip_exact(probe, local_start, local_end, name=f"find_{index:03d}")
        return clip
    except Exception as exc:
        logger.info(
            "Visual match materialization failed for %s S%s E%s: %s",
            candidate.get("season"), candidate.get("episode"), exc,
        )
        return None
    finally:
        if server is not None:
            await server.close()


async def _try_candidate_visual(client, candidate, segment, target_video, job_dir, index):
    chat, message_id = parse_telegram_message_link(candidate["source_url"])
    try:
        _, source_duration, _ = await get_telegram_video_info(client, chat, message_id)
    except Exception as exc:
        logger.info("Source metadata failed: %s", exc)
        return None

    match = await find_visual_match(
        client=client,
        candidate=candidate,
        segment=segment,
        target_video=target_video,
        job_dir=job_dir,
        source_duration=source_duration,
    )
    if not match:
        return None

    # Local retrieval is deliberately permissive; Gemini is the final judge for
    # the one strongest visual candidate. This prevents false positives while
    # avoiding Gemini uploads for every remote search window.
    if match["score"] > 0.55 or match["progression"] < 0.50:
        logger.info(
            "Visual candidate rejected score=%.3f progression=%.3f candidate=%s S%s E%s",
            match["score"], match["progression"], candidate["anime"],
            candidate["season"], candidate["episode"],
        )
        return None

    probe = job_dir / (
        f"verify_{index:03d}_{int(float(match['start']) * 1000)}_"
        f"{candidate['season']}_{candidate['episode']}.mp4"
    )
    server = None
    try:
        server = await open_telegram_range_server(client, candidate["source_url"])
        await _extract_remote_window(
            server,
            probe,
            max(0.0, float(match["start"])),
            max(2.0, float(match["end"]) - float(match["start"])),
        )
        verification = await verify_candidate_window(
            candidate_video_path=probe,
            segment=segment,
            candidate_start=float(match["start"]),
            candidate_end=float(match["end"]),
            target_video_path=target_video,
        )
        if not bool(verification.get("match")) or float(verification.get("confidence", 0.0)) < 0.70:
            logger.info(
                "Gemini verification rejected candidate=%s S%s E%s confidence=%s reason=%s",
                candidate["anime"], candidate["season"], candidate["episode"],
                verification.get("confidence"), verification.get("reason"),
            )
            return None

        verified_start = float(verification.get("start_time", 0.0))
        verified_end = float(verification.get("end_time", verified_start + 1.0))
        verified_start = max(0.0, min(verified_start, probe.stat().st_size and 10**9))
        verified_end = max(verified_start + 0.2, verified_end)
        clip = await make_clip_exact(
            probe,
            verified_start,
            min(verified_end, await _probe_duration(probe)),
            name=f"find_{index:03d}",
        )
        logger.info(
            "Gemini verified candidate score=%.3f confidence=%.3f source=%s S%s E%s q=%s reason=%s",
            match["score"], float(verification.get("confidence", 0.0)), candidate["anime"],
            candidate["season"], candidate["episode"], candidate["quality"],
            verification.get("reason"),
        )
        return clip
    except Exception as exc:
        logger.info("Gemini candidate verification failed: %s", exc)
        return None
    finally:
        if server is not None:
            await server.close()
        probe.unlink(missing_ok=True)


async def _probe_duration(path):
    from ffmpeg_utils import get_duration
    return await get_duration(path)


async def _make_target_segment(input_video, segment, job_dir, index):
    start = max(0.0, float(segment["start_time"]))
    duration = max(0.2, float(segment["end_time"]) - start)
    target = job_dir / f"target_{index:03d}.mp4"
    await run_command(
        FFMPEG_BIN, "-y", "-ss", str(start), "-i", str(input_video), "-t", str(duration),
        "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(target),
    )
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("Target edit scene extraction failed.")
    return target


async def _process_segment(client, segment, index, job_dir, input_video):
    anime = segment.get("anime")
    if not anime:
        logger.info("Scene %s skipped: Gemini did not identify an anime", index)
        return None

    target_video = await _make_target_segment(input_video, segment, job_dir, index)
    candidates = candidate_episodes(
        anime,
        segment.get("season"),
        segment.get("episode"),
        segment=segment,
    )
    if not candidates:
        target_video.unlink(missing_ok=True)
        return None

    tried_episodes = set()
    try:
        for candidate in candidates:
            episode_key = (candidate["season"], candidate["episode"])
            if episode_key in tried_episodes:
                continue
            tried_episodes.add(episode_key)
            logger.info(
                "Visual search %s S%s E%s quality=%s",
                candidate["anime"], candidate["season"], candidate["episode"], candidate["quality"],
            )
            clip = await _try_candidate_visual(
                client=client,
                candidate=candidate,
                segment=segment,
                target_video=target_video,
                job_dir=job_dir,
                index=index,
            )
            if clip:
                return {
                    "index": index,
                    "clip": clip,
                    "confidence": 0.90,
                    "reason": "Visual retrieval followed by Gemini final verification.",
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
                    "Template/overlay regions masked. Fast visual source search start..."
                )
            except Exception:
                pass

        semaphore = asyncio.Semaphore(2)

        async def worker(index, segment):
            async with semaphore:
                if progress_message:
                    try:
                        await progress_message.edit_text(
                            f"🎯 FIND\n\nScene {index}/{len(segments)}\n"
                            f"{segment.get('anime') or 'Unknown'} S{segment.get('season') or '?'} E{segment.get('episode') or '?'}\n\n"
                            "Fast visual retrieval + Gemini verification..."
                        )
                    except Exception:
                        pass
                try:
                    return await _process_segment(
                        client=telethon_client,
                        segment=segment,
                        index=index,
                        job_dir=job_dir,
                        input_video=input_video,
                    )
                except Exception as exc:
                    logger.exception("Scene %s failed: %s", index, exc)
                    return None

        gathered = await asyncio.gather(*(worker(i, segment) for i, segment in enumerate(segments, 1)))
        results = [result for result in gathered if result]
        results.sort(key=lambda result: result["index"])

        if not results:
            raise RuntimeError("Koi reliable source clip match nahi mila.")

        output = await merge_videos([result["clip"] for result in results], "find_result")
        return {"output": output, "matched": len(results), "total": len(segments)}
    finally:
        for item in results:
            try:
                item["clip"].unlink(missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(job_dir, ignore_errors=True)
