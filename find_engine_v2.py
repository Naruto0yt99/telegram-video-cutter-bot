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
REMOTE_HTTP_OPTIONS = ["-seekable", "1", "-multiple_requests", "1", "-initial_request_size", str(4*1024*1024), "-request_size", str(4*1024*1024), "-short_seek_size", str(4*1024*1024)]
MATCH_QUALITY_ORDER = ("720p", "1080p", "480p", "360p", "1440p", "2160p", "auto")


def _candidate_quality_sources(anime, season, episode):
    sources = get_all_sources_for_episode(anime, season, episode)
    return [{"anime": anime, "season": str(season), "episode": str(episode), "quality": q, "source_url": sources[q]} for q in MATCH_QUALITY_ORDER if q in sources]


def _episode_sort_key(value):
    try: return int(str(value))
    except Exception: return 10**9


def _landmark_episode_priority(anime, segment):
    name = str(anime or "").lower().replace("-", " ")
    if "naruto" not in name or "shippuden" in name: return []
    text = " ".join([str(segment.get("arc") or ""), " ".join(segment.get("landmarks") or []), str(segment.get("reason") or "")]).lower()
    if "forest of death" in text and ("anko" in text or "mitarashi" in text): return ["27", "28"]
    if "forest" in text and any(x in text for x in ("forest gate", "forest gates", "entering the forest", "second stage", "second exam")): return ["27", "28"]
    return []


def candidate_episodes(anime, season, episode, segment=None):
    if not anime: return []
    seasons = get_seasons(anime)
    requested_season, requested_episode = (str(season) if season is not None else None), (str(episode) if episode is not None else None)
    priority = _landmark_episode_priority(anime, segment or {})
    if requested_season in seasons and requested_episode:
        available = get_episodes(anime, requested_season)
        numbers = sorted(available, key=_episode_sort_key)
        ordered = [requested_episode] if requested_episode in available else []
        if requested_episode in numbers:
            pos = numbers.index(requested_episode); ordered.extend(numbers[max(0,pos-2):pos+3])
        result, seen = [], set()
        for ep in ordered:
            if ep not in seen: seen.add(ep); result.extend(_candidate_quality_sources(anime, requested_season, ep))
        return result
    result, seen = [], set()
    for current_season in seasons:
        available = get_episodes(anime, current_season)
        ordered = [ep for ep in priority if ep in available] + sorted(available, key=_episode_sort_key)
        for ep in ordered:
            key = (str(current_season), str(ep))
            if key not in seen: seen.add(key); result.extend(_candidate_quality_sources(anime, current_season, ep))
    return result


async def _extract_remote_window(server, output_path, source_start, duration):
    await run_command(FFMPEG_BIN, "-y", *REMOTE_HTTP_OPTIONS, "-ss", str(max(0.0,float(source_start))), "-i", server.url, "-t", str(max(1.0,float(duration))), "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart", str(output_path))


async def _probe_duration(path):
    from ffmpeg_utils import get_duration
    return await get_duration(path)


async def _verify_visual_candidate(client, candidate, target_video, job_dir, index, match):
    probe = job_dir / f"verify_{index:03d}_{candidate['season']}_{candidate['episode']}_{int(float(match['start'])*1000)}.mp4"
    server = None
    try:
        server = await open_telegram_range_server(client, candidate["source_url"])
        await _extract_remote_window(server, probe, max(0.0,float(match["start"])), max(2.0,float(match["end"])-float(match["start"])))
        if not probe.exists() or probe.stat().st_size == 0: return None
        context = {"anime":candidate["anime"],"season":candidate["season"],"episode":candidate["episode"],"quality":candidate["quality"],"retrieval_score":float(match.get("score",99.0)),"retrieval_progression":float(match.get("progression",0.0)),"retrieval_coverage":float(match.get("coverage",0.0)),"candidate_window_start":float(match["start"])}
        verification = await verify_candidate_window(candidate_path=probe, target_path=target_video, context=context)
        confidence = float(verification.get("confidence",0.0) or 0.0)
        if not bool(verification.get("match")) or confidence < 0.72: return None
        probe_duration = await _probe_duration(probe)
        verified_start = max(0.0,float(verification.get("start_time",0.0) or 0.0))
        verified_end = float(verification.get("end_time",verified_start+1.0) or (verified_start+1.0))
        verified_start = min(verified_start,max(0.0,probe_duration-0.2))
        verified_end = min(max(verified_start+0.2,verified_end),probe_duration)
        if verified_end <= verified_start: return None
        clip = await make_clip_exact(probe,verified_start,verified_end,name=f"find_{index:03d}")
        return {"clip":clip,"confidence":confidence,"candidate":candidate,"reason":verification.get("reason","Gemini visual verification passed.")}
    except Exception as exc:
        logger.info("Candidate verification failed %s S%s E%s q=%s: %s",candidate["anime"],candidate["season"],candidate["episode"],candidate["quality"],exc)
        return None
    finally:
        if server is not None: await server.close()
        probe.unlink(missing_ok=True)


async def _try_candidate_visual(client, candidate, segment, target_video, job_dir, index):
    chat, message_id = parse_telegram_message_link(candidate["source_url"])
    try: _, source_duration, _ = await get_telegram_video_info(client,chat,message_id)
    except Exception as exc:
        logger.info("Source metadata failed %s S%s E%s q=%s: %s",candidate["anime"],candidate["season"],candidate["episode"],candidate["quality"],exc); return None
    match = await find_visual_match(client=client,candidate=candidate,segment=segment,target_video=target_video,job_dir=job_dir,source_duration=source_duration)
    if not match: return None
    matches = sorted(match.get("candidates") or [match],key=lambda x:float(x.get("score",99.0)))[:4]
    for item in matches:
        if float(item.get("score",99.0)) > 0.70 or float(item.get("progression",0.0)) < 0.45 or float(item.get("coverage",0.0)) < 0.15: continue
        verified = await _verify_visual_candidate(client,candidate,target_video,job_dir,index,item)
        if verified: return verified
    return None


async def _make_target_segment(input_video, segment, job_dir, index):
    start=max(0.0,float(segment["start_time"])); duration=max(0.2,float(segment["end_time"])-start); target=job_dir/f"target_{index:03d}.mp4"
    await run_command(FFMPEG_BIN,"-y","-ss",str(start),"-i",str(input_video),"-t",str(duration),"-map","0:v:0?","-map","0:a:0?","-c:v","libx264","-preset","ultrafast","-crf","20","-c:a","aac","-movflags","+faststart",str(target))
    if not target.exists() or target.stat().st_size == 0: raise RuntimeError("Target edit scene extraction failed.")
    return target


async def _process_segment(client,segment,index,job_dir,input_video):
    anime=segment.get("anime")
    if not anime: return None
    target_video=await _make_target_segment(input_video,segment,job_dir,index)
    candidates=candidate_episodes(anime,segment.get("season"),segment.get("episode"),segment=segment)
    if not candidates:
        target_video.unlink(missing_ok=True); return None
    try:
        for candidate in candidates:
            logger.info("Visual search %s S%s E%s quality=%s",candidate["anime"],candidate["season"],candidate["episode"],candidate["quality"])
            verified=await _try_candidate_visual(client,candidate,segment,target_video,job_dir,index)
            if verified:
                return {"index":index,"clip":verified["clip"],"confidence":verified["confidence"],"reason":verified["reason"],"candidate":verified["candidate"]}
    finally: target_video.unlink(missing_ok=True)
    return None


async def find_and_build(input_video,user_id,telethon_client,progress_message=None):
    if telethon_client is None: raise RuntimeError("Telegram source client connected nahi hai.")
    input_video=Path(input_video)
    if not input_video.exists(): raise RuntimeError("Input video nahi mila.")
    segments=await analyze_video(input_video)
    if not segments: raise RuntimeError("Gemini ko koi usable anime segment nahi mila.")
    job_dir=Path(TEMP_DIR)/str(user_id)/"find_job"
    if job_dir.exists(): shutil.rmtree(job_dir,ignore_errors=True)
    job_dir.mkdir(parents=True,exist_ok=True); results=[]
    try:
        if progress_message:
            try: await progress_message.edit_text(f"🎯 FIND\n\nGemini analysis complete: {len(segments)} scenes\nMulti-candidate visual source search start...")
            except Exception: pass
        semaphore=asyncio.Semaphore(2)
        async def worker(index,segment):
            async with semaphore:
                try:
                    return await _process_segment(telethon_client,segment,index,job_dir,input_video)
                except Exception as exc:
                    logger.exception("Scene %s failed: %s",index,exc); return None
        gathered=await asyncio.gather(*(worker(i,s) for i,s in enumerate(segments,1)))
        results=[r for r in gathered if r]; results.sort(key=lambda r:r["index"])
        if not results: raise RuntimeError("Koi reliable source clip match nahi mila.")
        output=await merge_videos([r["clip"] for r in results],"find_result")
        return {"output":output,"matched":len(results),"total":len(segments)}
    finally:
        for item in results:
            try: item["clip"].unlink(missing_ok=True)
            except Exception: pass
        shutil.rmtree(job_dir,ignore_errors=True)
