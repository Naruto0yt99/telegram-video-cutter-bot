import asyncio
import logging
import re
from pathlib import Path

from config import FFMPEG_BIN, TEMP_DIR
from database import (
    get_all_sources_for_episode,
    get_all_sources_for_episode_any_season,
)
from ffmpeg_utils import run_command
from gemini_analyzer import analyze_video, verify_source_candidates
from telegram_remote import open_telegram_range_server
from utils import safe_filename, unique_path

logger = logging.getLogger("simple-find")


def _progress_bar(percent, width=20):
    percent = max(0, min(100, int(percent)))
    filled = int(round(width * percent / 100))
    return "█" * filled + "░" * (width - filled)


async def _show_find_progress(message, percent, title, detail=""):
    if message is None:
        return
    text = f"🎯 FIND — {percent}% [{_progress_bar(percent)}]\\n\\n{title}"
    if detail:
        text += f"\\n{detail}"
    try:
        await message.edit_text(text)
    except Exception:
        pass


QUALITY_ORDER = ("2160p", "1440p", "1080p", "720p", "480p", "360p", "auto")


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

    # Never silently change the season Gemini identified. A fallback to
    # another season can spend minutes searching the wrong episode and can
    # produce a false-positive clip.
    if not sources:
        by_season = get_all_sources_for_episode_any_season(anime, episode)
        if by_season:
            logger.warning(
                "Scene source unavailable for exact season: anime=%r S%s E%s; "
                "indexed episode exists in seasons=%s; refusing season fallback",
                anime,
                season,
                episode,
                sorted(by_season.keys()),
            )

    if not sources:
        return None, anime, resolved_season, episode, None

    for quality in QUALITY_ORDER:
        if quality in sources:
            return sources[quality], anime, resolved_season, episode, quality

    quality, source = next(iter(sources.items()))
    return source, anime, resolved_season, episode, quality


async def _extract_remote_clip(client, source_url, start, end, output, speed=1.0):
    """Extract only the requested Telegram interval; preserve edit speed when needed."""
    server = await open_telegram_range_server(client, source_url)
    try:
        start = max(0.0, float(start))
        source_duration = max(0.5, float(end) - start)
        speed = max(0.25, min(float(speed or 1.0), 4.0))

        common = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-seekable", "1", "-multiple_requests", "1",
            "-initial_request_size", "2M", "-request_size", "2M",
            "-short_seek_size", "4M", "-ss", str(start),
            "-i", server.url, "-t", str(source_duration),
            "-map", "0:v:0?", "-map", "0:a:0?",
        ]

        if abs(speed - 1.0) < 0.03:
            await run_command(
                *common,
                "-c:v", "copy", "-c:a", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", str(output),
            )
        else:
            # speed = original duration / edited duration.
            # Compress/expand the source interval so its playback matches the edit.
            atempo = speed
            audio_filters = []
            while atempo > 2.0:
                audio_filters.append("atempo=2.0")
                atempo /= 2.0
            while atempo < 0.5:
                audio_filters.append("atempo=0.5")
                atempo /= 0.5
            audio_filters.append(f"atempo={atempo:.6f}")

            await run_command(
                *common,
                "-vf", f"setpts=PTS/{speed:.8f}",
                "-af", ",".join(audio_filters),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", str(output),
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


async def _verify_region(input_video, client, source_url, region, output_dir, progress_message=None, scene_label=""):
    try:
        hint = float(region.get("source_start_hint"))
    except (TypeError, ValueError):
        return None

    edit_start = float(region["start_time"])
    edit_length = max(0.8, float(region["end_time"]) - edit_start)
    sample_len = min(8.0, edit_length)
    edit_sample = output_dir / f"verify_edit_{int(edit_start * 1000)}.mp4"
    await _make_edit_sample(input_video, edit_start, sample_len, edit_sample)

    # Do not call Gemini once per probe. The old 9-probe loop uploaded and
    # processed a new source video on every attempt, so one scene could spend
    # many minutes waiting on repeated Gemini file processing. Build a small
    # set of candidate windows locally, then let one Gemini request compare all
    # candidates against the edit sample.
    centers = [0, -120, 120, -300, 300, -600, 600]
    probe_duration = max(12.0, min(16.0, edit_length * 2.0))
    candidates = []

    async def extract_candidate(probe_index, offset):
        center = max(0.0, hint + offset)
        ws = max(0.0, center - probe_duration / 2)
        src = output_dir / f"verify_source_{int(ws)}_{probe_index}.mp4"
        await _show_find_progress(
            progress_message,
            15,
            f"🔎 {scene_label} — exact source search",
            f"Candidate {probe_index}/{len(centers)} • around {ws:.0f}s",
        )
        try:
            await _extract_remote_clip(client, source_url, ws, ws + probe_duration, src)
            return {"index": probe_index, "start": ws, "path": src}
        except Exception:
            logger.exception("Candidate extraction failed probe=%s around=%s", probe_index, ws)
            return None

    try:
        # Limit remote Telegram work so the phone does not open seven large
        # range/FFmpeg operations at once.
        sem = asyncio.Semaphore(3)

        async def guarded(i, offset):
            async with sem:
                return await extract_candidate(i, offset)

        extracted = await asyncio.gather(
            *(guarded(i, offset) for i, offset in enumerate(centers, 1))
        )
        candidates = [x for x in extracted if x]
        if not candidates:
            return None

        result = await asyncio.to_thread(
            verify_source_candidates,
            edit_sample,
            [x["path"] for x in candidates],
            [x["start"] for x in candidates],
        )
        if not result or not result.get("match"):
            return None

        candidate_index = int(result.get("candidate_index", 0) or 0)
        selected = next((x for x in candidates if x["index"] == candidate_index), None)
        if selected is None:
            return None

        conf = float(result.get("confidence", 0) or 0)
        off = float(result.get("offset_in_candidate", 0) or 0)
        sd = float(result.get("source_duration", 0) or 0)
        sp = float(result.get("speed", 1) or 1)
        return {
            "start": max(0.0, selected["start"] + off),
            "source_duration": max(0.5, sd or edit_length * sp),
            "speed": max(0.25, min(sp, 4.0)),
            "confidence": conf,
        }
    finally:
        edit_sample.unlink(missing_ok=True)
        for candidate in candidates:
            candidate["path"].unlink(missing_ok=True)


async def _process_fast_scene(input_video, telethon_client, region, index, total, output_dir, progress_message=None):
    source, anime, season, episode, quality = _source_for_region(region)
    if not source: return None
    edit_start=float(region["start_time"]); edit_end=float(region["end_time"]); edit_length=max(.5,edit_end-edit_start)
    match=await _verify_region(input_video,telethon_client,source,region,output_dir,progress_message, f"Scene {index}/{total}")
    if not match: return None
    start=match["start"]; source_duration=match["source_duration"]; speed=match["speed"]
    end=start+source_duration
    output=unique_path(output_dir,safe_filename(f"find_{index:02d}_{anime}_S{season}E{episode}_{int(start)}")+".mp4")
    await _extract_remote_clip(telethon_client,source,start,end,output,speed=speed)
    return {"path":output,"index":index,"anime":anime,"season":season,"episode":episode,"start":start,"end":end,"edit_start":edit_start,"edit_end":edit_end,"edit_duration":edit_length,"speed":speed,"quality":quality,"confidence":match["confidence"]}

async def find_and_build(input_video, user_id, telethon_client, progress_message=None):
    if telethon_client is None: raise RuntimeError("Telegram source client connected nahi hai.")
    input_video=Path(input_video)
    if not input_video.exists(): raise RuntimeError("Input video nahi mila.")
    if progress_message:
        try: await progress_message.edit_text("🎯 FIND — 15%\n\n🧠 Gemini shot-by-shot analysis...")
        except Exception: pass
    regions=await analyze_video(input_video)
    if not regions: raise RuntimeError("Gemini ko koi usable anime scene nahi mila.")
    output_dir=Path(TEMP_DIR)/str(user_id)/"find_clips"; output_dir.mkdir(parents=True,exist_ok=True)
    semaphore=asyncio.Semaphore(3)
    completed = 0
    progress_lock = asyncio.Lock()
    total_regions = len(regions)

    async def worker(i, r):
        nonlocal completed
        async with semaphore:
            try:
                await _show_find_progress(
                    progress_message,
                    15,
                    f"🔎 Scene {i}/{total_regions} — exact source search",
                    f"📺 {r.get('anime','?')} S{r.get('season','?')} E{r.get('episode','?')}\n⚙️ Progressive targeted probes...",
                )
                return await _process_fast_scene(input_video, telethon_client, r, i, total_regions, output_dir, progress_message)
            except Exception:
                logger.exception("Scene %s failed", i)
                return None
            finally:
                async with progress_lock:
                    completed += 1
                    percent = 15 + int(75 * completed / total_regions)
                    await _show_find_progress(
                        progress_message,
                        percent,
                        f"🧩 Scenes processed: {completed}/{total_regions}",
                        "⏳ Remaining scenes are still being searched...",
                    )

    results = await asyncio.gather(*(worker(i, r) for i, r in enumerate(regions, 1)))
    clips=sorted([x for x in results if x],key=lambda x:x["index"])
    if not clips: raise RuntimeError("Koi scene reliably match nahi hua.")
    await _show_find_progress(progress_message, 90, "🎬 Scene search complete", f"Matched {len(clips)}/{len(regions)} scenes\n🔧 Building the final merged video...")
    merged = unique_path(output_dir, "find_final.mp4")

    # concat demuxer requires matching stream parameters. Normalize every
    # matched scene to the highest quality represented in the result before
    # concatenation, while preserving each scene's exact timeline.
    quality_rank = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "auto": 720}
    target_height = max(quality_rank.get(str(x.get("quality", "auto")), 720) for x in clips)
    target_width = int(round(target_height * 16 / 9))
    normalized = []

    for x in clips:
        src = Path(x["path"])
        dst = unique_path(output_dir, f"normalized_{x['index']:02d}.mp4")
        await run_command(
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-i", str(src),
            "-vf", f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                   f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-r", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(dst),
        )
        normalized.append(dst)

    list_path = output_dir / "concat.txt"
    list_path.write_text(
        "\n".join(
            "file '" + str(p).replace("'", "'\\''") + "'"
            for p in normalized
        ),
        encoding="utf-8",
    )
    try:
        await run_command(
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart", str(merged),
        )
    finally:
        list_path.unlink(missing_ok=True)
        for p in normalized:
            p.unlink(missing_ok=True)
    report=["📋 SCENE DETAILS",""]
    for x in clips:
        report += [f"{x['index']:02d} │ {x['anime']} S{x['season']} E{x['episode']}",f"   EDIT {x['edit_start']:.2f}s → {x['edit_end']:.2f}s",f"   RAW  {x['start']:.2f}s → {x['end']:.2f}s",f"   ⚡ Speed: {x['speed']:.2f}×",f"   🎯 Confidence: {x['confidence']:.0%}",""]
    return {"clips":clips,"matched":len(clips),"total":len(regions),"regions":regions,"output":merged,"report":"\n".join(report),"sources":{(str(x.get('anime','')).strip().lower(),_number(x.get('season')),_number(x.get('episode'))) for x in regions}}
