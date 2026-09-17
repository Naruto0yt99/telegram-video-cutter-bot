import asyncio
import logging
import shutil
from pathlib import Path

from database import get_seasons, get_episodes, get_all_sources_for_episode
from telegram_media import parse_telegram_message_link
from telegram_remote import open_telegram_range_server, get_telegram_video_info
from ffmpeg_utils import run_command, make_clip_exact, merge_videos
from config import TEMP_DIR, FFMPEG_BIN
from gemini_analyzer import analyze_video
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
        {"anime": anime, "season": str(season), "episode": str(episode), "quality": q, "source_url": sources[q]}
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


def candidate_episodes(anime, season, episode, segment=None):
    if not anime:
        return []

    requested_season = str(season) if season is not None else None
    requested_episode = str(episode) if episode is not None else None
    priority_episodes = _landmark_episode_priority(anime, segment or {})

    seasons = get_seasons(anime)
    if requested_season in seasons:
        ordered_seasons = [requested_season] + [s for s in seasons if s != requested_season]
    else:
        ordered_seasons = seasons

    candidates = []
    seen = set()

    def add_episode(current_season, current_episode):
        key = (str(anime).lower(), str(current_season), str(current_episode))
        if key in seen:
            return
        seen.add(key)
        candidates.extend(_candidate_quality_sources(anime, current_season, current_episode))

    for current_season in ordered_seasons:
        if requested_season is not None and current_season != requested_season:
            continue
        available = get_episodes(anime, current_season)
        for prioritized in priority_episodes:
            if prioritized in available:
                add_episode(current_season, prioritized)
        if requested_episode is not None and requested_episode in available:
            add_episode(current_season, requested_episode)

    for current_season in ordered_seasons:
        for current_episode in get_episodes(anime, current_season):
            add_episode(current_season, current_episode)

    logger.info(
        "Candidate episodes for %s S%s E%s: %s source variants landmarks=%s",
        anime, season if season is not None else "?", episode if episode is not None else "?",
        len(candidates), priority_episodes,
    )
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


async def _try_candidate_visual(client, candidate, segment, job_dir, index):
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
        target_video=job_dir / f"target_{index:03d}.mp4",
        job_dir=job_dir,
        source_duration=source_duration,
    )
    if not match:
        return None

    if match["score"] > 0.52 or match["progression"] < 0.55:
        logger.info(
            "Visual candidate rejected score=%.3f progression=%.3f candidate=%s S%s E%s",
            match["score"], match["progression"], candidate["anime"],
            candidate["season"], candidate["episode"],
        )
        return None

    logger.info(
        "Visual candidate accepted score=%.3f progression=%.3f source=%.2f-%.2f %s S%s E%s q=%s",
        match["score"], match["progression"], match["start"], match["end"],
        candidate["anime"], candidate["season"], candidate["episode"], candidate["quality"],
    )
    return await _materialize_match(client, candidate, match, segment, job_dir, index)


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
    candidates = candidate_episodes(anime, segment.get("season"), segment.get("episode"), segment=segment)
    if not candidates:
        target_video.unlink(missing_ok=True)
        return None

    tried_episodes = set()
    try:
        # Each episode is searched once, using the lightest available quality. This
        # prevents a failed episode from multiplying the remote search by 720/1080/etc.
        for candidate in candidates:
            episode_key = (candidate["season"], candidate["episode"])
            if episode_key in tried_episodes:
                continue
            tried_episodes.add(episode_key)
            logger.info(
                "Visual search %s S%s E%s quality=%s",
                candidate["anime"], candidate["season"], candidate["episode"], candidate["quality"],
            )
            clip = await _try_candidate_visual(client, candidate, segment, job_dir, index)
            if clip:
                return {
                    "index": index,
                    "clip": clip,
                    "confidence": 0.80,
                    "reason": "Local visual retrieval match; Gemini used only for scene mapping and edit-region masking.",
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
                    "Template/overlay regions masked. Local visual source search start..."
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
                            "Local visual retrieval: Telegram remote range search..."
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
