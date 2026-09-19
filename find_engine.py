import asyncio
import logging
import re
import uuid
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

_progress_state = {}
_progress_lock = asyncio.Lock()

# Limit total remote FFmpeg/range-server work across all scenes. FIND already
# searches multiple scenes in parallel; an additional per-scene limit could
# otherwise create 9+ simultaneous Telegram range streams on a phone.
REMOTE_EXTRACTION_SEMAPHORE = asyncio.Semaphore(4)


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

    # Several scenes/candidates report progress concurrently. Throttle status
    # edits so Telegram rate limits cannot become the thing that breaks FIND.
    key = id(message)
    async with _progress_lock:
        now = asyncio.get_running_loop().time()
        previous = _progress_state.get(key)
        if previous:
            previous_time, previous_text = previous
            if text == previous_text:
                return
            if now - previous_time < 0.75 and int(percent) < 90:
                return
        try:
            await message.edit_text(text)
            _progress_state[key] = (now, text)
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

    # If the exact season is not indexed, a single indexed season is still
    # safe to try because the visual verification step below must confirm the
    # actual scene. This is especially important for libraries whose uploader
    # labels seasons differently from Gemini's canonical numbering.
    if not sources:
        by_season = get_all_sources_for_episode_any_season(anime, episode)
        if len(by_season) == 1:
            resolved_season = next(iter(by_season))
            sources = by_season[resolved_season]
            logger.warning(
                "Exact season unavailable: anime=%r Gemini=S%s E%s; "
                "using only indexed season S%s for visual verification",
                anime,
                season,
                episode,
                resolved_season,
            )
        elif by_season:
            logger.warning(
                "Exact season unavailable: anime=%r S%s E%s; "
                "multiple indexed seasons=%s, refusing ambiguous fallback",
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
    async with REMOTE_EXTRACTION_SEMAPHORE:
        return await _extract_remote_clip_limited(client, source_url, start, end, output, speed)


async def _extract_remote_clip_limited(client, source_url, start, end, output, speed=1.0):
    server = await open_telegram_range_server(client, source_url)
    try:
        start = max(0.0, float(start))
        end = max(start + 0.05, float(end))
        # Clamp remote seeks to the real episode duration so a bad Gemini
        # timestamp near/after EOF cannot make FFmpeg fail before extraction.
        max_start = max(0.0, float(server.duration) - 0.05)
        start = min(start, max_start)
        end = min(end, float(server.duration))
        if end <= start:
            raise RuntimeError("Requested source interval is outside the episode.")
        source_duration = max(0.05, end - start)
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
    # Every scene runs concurrently, so filenames must be unique even when
    # two scenes start at the same edit timestamp or probe the same source time.
    scene_token = f"{int(edit_start * 1000)}_{uuid.uuid4().hex[:8]}"
    edit_sample = output_dir / f"verify_edit_{scene_token}.mp4"
    await _make_edit_sample(input_video, edit_start, sample_len, edit_sample)

    # Start with the most likely windows. Only expand to the wider +/-5/10 min
    # probes when the fast pass does not produce a sufficiently confident match.
    probe_duration = max(12.0, min(16.0, edit_length * 2.0))
    probe_batches = (
        (0, -120, 120),
        (-300, 300, -600, 600),
    )
    candidates = []
    result = None

    async def extract_candidate(probe_index, offset, batch_total):
        center = max(0.0, hint + offset)
        ws = max(0.0, center - probe_duration / 2)
        src = output_dir / f"verify_source_{scene_token}_{int(ws)}_{probe_index}.mp4"
        await _show_find_progress(
            progress_message,
            15,
            f"🔎 {scene_label} — exact source search",
            f"Candidate {probe_index}/{batch_total} • around {ws:.0f}s",
        )
        try:
            await _extract_remote_clip(client, source_url, ws, ws + probe_duration, src)
            return {"index": probe_index, "start": ws, "path": src}
        except Exception:
            logger.exception("Candidate extraction failed probe=%s around=%s", probe_index, ws)
            return None

    async def run_batch(offsets, index_base):
        batch_total = len(offsets)
        sem = asyncio.Semaphore(3)

        async def guarded(i, offset):
            async with sem:
                return await extract_candidate(index_base + i, offset, batch_total)

        extracted = await asyncio.gather(
            *(guarded(i, offset) for i, offset in enumerate(offsets, 1))
        )
        return [x for x in extracted if x]

    def usable_result(result):
        if not result or not result.get("match"):
            return False
        try:
            return float(result.get("confidence", 0) or 0) >= 0.60
        except (TypeError, ValueError):
            return False

    try:
        # Fast pass: 3 nearby candidates. This avoids uploading/processing all
        # seven windows for the common case where Gemini's timestamp hint is close.
        candidates = await run_batch(probe_batches[0], 0)
        if candidates:
            result = await verify_source_candidates(
                edit_sample,
                [x["path"] for x in candidates],
                [x["start"] for x in candidates],
            )
            logger.info(
                "FIND source verification fast pass scene=%s match=%s confidence=%s candidate=%s",
                scene_label,
                bool(result and result.get("match")),
                result.get("confidence") if result else None,
                result.get("candidate_index") if result else None,
            )
            if usable_result(result):
                candidate_index = int(result.get("candidate_index", 0) or 0)
                selected = candidates[candidate_index - 1] if 1 <= candidate_index <= len(candidates) else None
                if selected is not None:
                    off = float(result.get("offset_in_candidate", 0) or 0)
                    sp = max(0.25, min(float(result.get("speed", 1) or 1), 4.0))
                    return {
                        "start": max(0.0, selected["start"] + max(0.0, min(off, probe_duration - 0.5))),
                        # Gemini can occasionally return a wildly wrong duration.
                        # The edited scene length and speed are sufficient to derive
                        # the required original interval, so keep those authoritative.
                        "source_duration": max(0.5, edit_length * sp),
                        "speed": sp,
                        "confidence": float(result.get("confidence", 0) or 0),
                    }

        # Wide pass: if Gemini saw a possible match but was not confident,
        # retry with all seven windows so the nearby candidate is not discarded.
        # If there was no match at all, use only the four wider offsets.
        had_low_confidence_match = bool(
            result and result.get("match")
        )
        for candidate in candidates:
            candidate["path"].unlink(missing_ok=True)
        if had_low_confidence_match:
            candidates = await run_batch(probe_batches[0] + probe_batches[1], 0)
        else:
            candidates = await run_batch(probe_batches[1], 3)
        if candidates:
            result = await verify_source_candidates(
                edit_sample,
                [x["path"] for x in candidates],
                [x["start"] for x in candidates],
            )
            logger.info(
                "FIND source verification wide pass scene=%s match=%s confidence=%s candidate=%s",
                scene_label,
                bool(result and result.get("match")),
                result.get("confidence") if result else None,
                result.get("candidate_index") if result else None,
            )
            if result and result.get("match"):
                candidate_index = int(result.get("candidate_index", 0) or 0)
                selected = candidates[candidate_index - 1] if 1 <= candidate_index <= len(candidates) else None
                if selected is not None:
                    conf = float(result.get("confidence", 0) or 0)
                    off = float(result.get("offset_in_candidate", 0) or 0)
                    sp = max(0.25, min(float(result.get("speed", 1) or 1), 4.0))
                    return {
                        "start": max(0.0, selected["start"] + max(0.0, min(off, probe_duration - 0.5))),
                        "source_duration": max(0.5, edit_length * sp),
                        "speed": sp,
                        "confidence": conf,
                    }
        return None
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

    # Fast path: when all matched clips already have identical stream
    # parameters, concatenate with stream-copy. This avoids any re-encoding.
    async def probe_stream_signature(path):
        out, _ = await run_command(
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", str(path),
        )
        import json
        data = json.loads(out or "{}")
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not video or not audio:
            return None
        return (
            video.get("codec_name"), video.get("profile"), video.get("pix_fmt"),
            video.get("width"), video.get("height"), video.get("r_frame_rate"),
            video.get("sample_aspect_ratio"), video.get("time_base"),
            audio.get("codec_name"), audio.get("sample_rate"), audio.get("channels"),
            audio.get("channel_layout"), audio.get("time_base"),
        )

    signatures = await asyncio.gather(*(probe_stream_signature(x["path"]) for x in clips))
    same_streams = bool(signatures) and all(sig is not None and sig == signatures[0] for sig in signatures)

    if same_streams:
        list_path = output_dir / "concat.txt"
        list_path.write_text(
            "\\n".join(
                "file '" + str(x["path"]).replace("'", "'\\''") + "'"
                for x in clips
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
    else:
        # Mixed source parameters need one re-encode. Do it in a SINGLE
        # filter/encode pass rather than normalizing every clip separately and
        # then concatenating them again; that was the main multi-hour bottleneck
        # on low-power Android devices.
        quality_rank = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "auto": 720}
        target_height = max(quality_rank.get(str(x.get("quality", "auto")), 720) for x in clips)
        target_width = int(round(target_height * 16 / 9))

        async def has_audio(path):
            out, _ = await run_command(
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            )
            return bool(out.strip())

        audio_flags = await asyncio.gather(*(has_audio(x["path"]) for x in clips))
        ff_args = [FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y"]
        for x in clips:
            ff_args += ["-i", str(x["path"])]

        filters = []
        concat_inputs = []
        for i, (x, has_a) in enumerate(zip(clips, audio_flags)):
            filters.append(
                f"[{i}:v:0]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
            if has_a:
                filters.append(f"[{i}:a:0]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
            else:
                duration = max(0.5, float(x.get("edit_duration", 0) or 0))
                filters.append(
                    f"anullsrc=channel_layout=stereo:sample_rate=48000:r=48000,"
                    f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{i}]"
                )
            concat_inputs.append(f"[v{i}][a{i}]")

        filters.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[vout][aout]")
        ff_args += [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "superfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(merged),
        ]
        await run_command(*ff_args)

    if not merged.exists() or merged.stat().st_size == 0:
        raise RuntimeError("Final merged video empty bana hai.")
    report=["📋 SCENE DETAILS",""]
    for x in clips:
        report += [f"{x['index']:02d} │ {x['anime']} S{x['season']} E{x['episode']}",f"   EDIT {x['edit_start']:.2f}s → {x['edit_end']:.2f}s",f"   RAW  {x['start']:.2f}s → {x['end']:.2f}s",f"   ⚡ Speed: {x['speed']:.2f}×",f"   🎯 Confidence: {x['confidence']:.0%}",""]
    return {"clips":clips,"matched":len(clips),"total":len(regions),"regions":regions,"output":merged,"report":"\n".join(report),"sources":{(str(x.get('anime','')).strip().lower(),_number(x.get('season')),_number(x.get('episode'))) for x in regions}}
