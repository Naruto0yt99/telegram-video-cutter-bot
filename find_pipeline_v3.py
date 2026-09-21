import asyncio
import json
import logging
import mimetypes
import re
from pathlib import Path

import httpx

from config import GEMINI_API_KEY, TEMP_DIR, FFMPEG_BIN
from database import get_all_sources_for_episode
from library_nav import canonical_anime
from telegram_remote import get_telegram_video_info, open_telegram_range_server
from ffmpeg_utils import run_command
from telegram_media import parse_telegram_message_link
from utils import safe_filename, unique_path

logger = logging.getLogger("find-pipeline-v3")

GEMINI_ROOT = "https://generativelanguage.googleapis.com"
# Requested model first; modern fallback keeps the pipeline usable if the legacy
# model is unavailable for the account.
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash")
CHUNK_BYTES = 512 * 1024
QUALITY_LOW_TO_HIGH = ("240p", "360p", "480p", "720p", "1080p", "1440p", "2160p", "auto")


def _headers():
    return {"x-goog-api-key": GEMINI_API_KEY}


async def _gemini_upload_path(path: Path):
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=180, write=180)) as client:
        r = await client.post(
            f"{GEMINI_ROOT}/upload/v1beta/files",
            headers={
                **_headers(),
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": path.name}},
        )
        r.raise_for_status()
        upload_url = r.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError("Gemini upload URL nahi mila.")
        async def body():
            with path.open("rb") as f:
                while True:
                    chunk = f.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk
        r = await client.post(
            upload_url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Type": mime,
            },
            content=body(),
        )
        r.raise_for_status()
        return r.json().get("file", {})


async def _gemini_upload_telegram(client, source_url: str, display_name: str):
    """Create a tiny visual proxy from Telegram and upload only that proxy to Gemini."""
    import os
    import tempfile

    chat, message_id = parse_telegram_message_link(source_url)
    _, duration, _ = await get_telegram_video_info(client, chat, message_id)

    server = await open_telegram_range_server(client, source_url)
    proxy = None
    try:
        fd, proxy = tempfile.mkstemp(prefix="gemini_proxy_", suffix=".mp4")
        os.close(fd)

        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-i", server.url,
            "-vf", "fps=2,scale=-2:240",
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "31",
            "-movflags", "+faststart",
            proxy,
        ]
        await run_command(*cmd)

        path = Path(proxy)
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError("Gemini visual proxy empty bana.")

        last_error = None
        for attempt in range(4):
            try:
                file_data = await _gemini_upload_path(path)
                if not file_data.get("name"):
                    raise RuntimeError("Gemini ne visual proxy ko file ke roop me accept nahi kiya.")
                return file_data, duration, path.stat().st_size
            except Exception as exc:
                last_error = exc
                if attempt >= 3:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise last_error or RuntimeError("Gemini proxy upload failed.")
    finally:
        await server.close()
        if proxy:
            try:
                os.remove(proxy)
            except OSError:
                pass

async def _wait_active(name: str):
    deadline = asyncio.get_running_loop().time() + 300
    async with httpx.AsyncClient(timeout=60) as client:
        while asyncio.get_running_loop().time() < deadline:
            r = await client.get(f"{GEMINI_ROOT}/v1beta/{name}", headers=_headers())
            r.raise_for_status()
            data = r.json()
            state = data.get("state") or data.get("file", {}).get("state")
            if state in (None, "ACTIVE"):
                return data
            if state == "FAILED":
                raise RuntimeError(f"Gemini video processing failed: {data}")
            await asyncio.sleep(2)
    raise TimeoutError("Gemini video processing timeout.")


def _json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


async def _resolve_file_uri(client, name):
    # Gemini File API returns a resource name AND a separate file.uri. The
    # generateContent file_data.file_uri must use the latter, not the REST
    # resource URL. Older code guessed the URL from name, which can make every
    # model reject the video and produce the misleading "no regions" fallback.
    r = await client.get(f"{GEMINI_ROOT}/v1beta/{name}", headers=_headers())
    r.raise_for_status()
    data = r.json()
    file_obj = data.get("file", data)
    uri = file_obj.get("uri")
    if not uri:
        raise RuntimeError(f"Gemini file URI missing for {name}")
    return uri

async def _generate(prompt, files):
    last = None
    timeout = httpx.Timeout(60, read=600, write=120)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            uris = [await _resolve_file_uri(client, name) for name in files]
        except Exception as exc:
            logger.warning("Gemini file URI resolution failed: %s", exc)
            raise

        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    *[
                        {
                            "file_data": {
                                "mime_type": "video/mp4",
                                "file_uri": uri,
                            }
                        }
                        for uri in uris
                    ],
                    {"text": prompt},
                ],
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
            },
        }

        for model in GEMINI_MODELS:
            try:
                r = await client.post(
                    f"{GEMINI_ROOT}/v1beta/models/{model}:generateContent",
                    headers={**_headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                text_parts = []
                for candidate in data.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        if part.get("text"):
                            text_parts.append(part["text"])
                return _json("\n".join(text_parts))
            except Exception as exc:
                last = exc
                logger.warning("Gemini model failed: %s: %s", model, exc)
    raise last or RuntimeError("Gemini request failed.")


def _qualities(sources):
    return [q for q in QUALITY_LOW_TO_HIGH if q in sources]


def _source_pair(anime, season, episode):
    sources = get_all_sources_for_episode(anime, season, episode)
    if not sources:
        return None, None, {}
    qs = _qualities(sources)
    if not qs:
        return next(iter(sources.values())), next(iter(sources.values())), sources
    low_q = next((q for q in qs if q in ("240p", "360p")), qs[0])
    high_q = next((q for q in reversed(qs) if q != "auto"), qs[-1])
    return sources[low_q], sources[high_q], sources


async def _extract_high_res(client, source_url, start, end, output):
    server = await open_telegram_range_server(client, source_url)
    try:
        await run_command(
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-ss", f"{float(start):.3f}",
            "-i", server.url,
            "-t", f"{max(0.05, float(end) - float(start)):.3f}",
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output),
        )
    finally:
        await server.close()
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("High-quality stream-copy clip empty bana.")
    return output


async def run_find_v3(input_video: Path, user_id: int, telegram_client, progress_message=None):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing.")
    if telegram_client is None:
        raise RuntimeError("Telegram USER_SESSION connected nahi hai.")

    job_dir = Path(TEMP_DIR) / str(user_id) / "find_v3"
    job_dir.mkdir(parents=True, exist_ok=True)

    async def progress(text):
        if progress_message:
            try:
                await progress_message.edit_text(text)
            except Exception:
                pass

    await progress("🎯 FIND — 10%\n\n🧠 YouTube Short ko Gemini analyse kar raha hai...")
    edit_file = await _gemini_upload_path(Path(input_video))
    await _wait_active(edit_file["name"])

    # First pass identifies the source anime/episode candidates and the visual
    # evidence that will be used for the direct episode comparison.
    analysis = await _generate(
        """Watch VIDEO 1, the YouTube Short carefully. This is an evidence-first
scene identification pass. Return EVERY contiguous anime shot, even when the
exact season or episode is uncertain.

For each shot return:
- start_time and end_time in the Short
- anime title (best identification; never use generic labels)
- season and episode ONLY when supported by visible evidence
- distinctive chronological visual description
- characters, location/background, important actions and visual landmarks
- source_start_hint only when you are genuinely confident

Do NOT return an empty regions list just because episode/timestamp is uncertain.
Do NOT invent an episode number. The next stage will verify the scene against the
actual Telegram episode.

Return JSON only:
{"regions":[{"start_time":0,"end_time":5,"anime":"Naruto","season":1,"episode":27,
"source_start_hint":null,"description":"specific chronological action, characters,
background and camera movement","characters":["..."],"location":"...","landmarks":["..."]}]}""",
        [edit_file["name"]],
    )
    regions = analysis.get("regions") if isinstance(analysis, dict) else None
    if not regions:
        raise RuntimeError("Gemini ko Short me koi usable scene nahi mila.")

    await progress(f"🎯 FIND — 25%\n\n📚 {len(regions)} scenes identify ho gaye.\n🔎 Telegram library se exact source episode locate ho raha hai...")

    # Group by episode so an episode is streamed to Gemini only once even if
    # several Short scenes came from it.
    groups = {}
    for r in regions:
        anime = canonical_anime(str(r.get("anime") or "").strip()) or str(r.get("anime") or "").strip()
        season = r.get("season")
        episode = r.get("episode")
        try:
            season = int(season)
            episode = int(episode)
        except (TypeError, ValueError):
            continue
        low, high, sources = _source_pair(anime, season, episode)
        if low and high:
            groups.setdefault((anime, season, episode), (low, high, sources))

    if not groups:
        raise RuntimeError("Gemini ke identified anime/episodes Telegram library me nahi mile.")

    episode_files = {}
    for n, ((anime, season, episode), (low, high, _)) in enumerate(groups.items(), 1):
        await progress(
            f"🎯 FIND — {25 + int(30*n/max(1,len(groups)))}%\n\n"
            f"📺 {anime} S{season} E{episode}\n"
            "🎞️ 240p/2fps compact visual proxy → Gemini..."
        )
        file_data, duration, size = await _gemini_upload_telegram(
            telegram_client,
            low,
            f"{safe_filename(anime)}_S{season:02d}E{episode:03d}_low.mp4",
        )
        await _wait_active(file_data["name"])
        episode_files[(anime, season, episode)] = {
            "gemini_name": file_data["name"],
            "low": low,
            "high": high,
            "duration": duration,
            "size": size,
        }

    await progress("🎯 FIND — 60%\n\n🧠 Gemini ab Short ko actual Telegram episodes se compare kar raha hai...")

    results = []
    for idx, region in enumerate(regions, 1):
        anime = canonical_anime(str(region.get("anime") or "").strip()) or str(region.get("anime") or "").strip()
        try:
            key = (anime, int(region.get("season")), int(region.get("episode")))
        except (TypeError, ValueError):
            continue
        ep = episode_files.get(key)
        if not ep:
            continue

        prompt = f"""VIDEO 1 is an edited anime shot from a YouTube Short.
VIDEO 2 is the LOW-QUALITY ORIGINAL EPISODE from the user's Telegram library.
Find the exact original interval in VIDEO 2 that visually corresponds to VIDEO 1.

Edited shot timing: {float(region.get('start_time', 0)):.3f} to {float(region.get('end_time', 0)):.3f}.
Known anime/episode: {anime} S{key[1]} E{key[2]}.
Visual description from the first pass: {region.get('description','')}

Return JSON only:
{{"match":true,"start":0.0,"end":0.0,"confidence":0.0,"speed":1.0}}
start/end are ORIGINAL EPISODE seconds, not Short seconds.
Use exact visible action/continuity. Account for intro offsets, release timing,
speed changes, crops, subtitles and transitions. Do not match merely because
characters are similar. Confidence below 0.80 means match=false."""
        match = await _generate(prompt, [edit_file["name"], ep["gemini_name"]])
        if not isinstance(match, dict) or not match.get("match"):
            continue
        start = float(match.get("start", 0) or 0)
        end = float(match.get("end", 0) or 0)
        if end <= start:
            continue
        results.append({
            "index": idx,
            "anime": anime,
            "season": key[1],
            "episode": key[2],
            "start": start,
            "end": end,
            "edit_start": float(region.get("start_time", 0) or 0),
            "edit_end": float(region.get("end_time", 0) or 0),
            "speed": float(match.get("speed", 1.0) or 1.0),
            "confidence": float(match.get("confidence", 0) or 0),
            "high": ep["high"],
        })

    if not results:
        raise RuntimeError("Gemini ne Telegram episode me koi reliable exact interval confirm nahi kiya.")

    results.sort(key=lambda x: x["edit_start"])
    clips = []
    await progress(f"🎯 FIND — 80%\n\n✂️ {len(results)} exact intervals mil gaye.\n⚡ Highest-quality Telegram stream se stream-copy cutting...")

    for i, item in enumerate(results, 1):
        output = job_dir / f"clip_{i:03d}.mp4"
        await _extract_high_res(
            telegram_client, item["high"], item["start"], item["end"], output
        )
        item["path"] = output
        clips.append(output)

    merged = job_dir / "final.mp4"
    concat = job_dir / "concat.txt"
    concat.write_text(
        "\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p in clips),
        encoding="utf-8",
    )
    try:
        await run_command(
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c", "copy", "-movflags", "+faststart", str(merged),
        )
    finally:
        concat.unlink(missing_ok=True)

    await progress("🎯 FIND — 95%\n\n📦 Final high-quality clip ready.\n📤 Telegram par upload ho raha hai...")
    report = "\n".join(
        f"{x['index']:02d} | {x['anime']} S{x['season']} E{x['episode']} | "
        f"RAW {x['start']:.3f}s → {x['end']:.3f}s | confidence {x['confidence']:.0%}"
        for x in results
    )
    return {
        "output": merged,
        "clips": [
            {
                "path": x["path"],
                "index": x["index"],
                "anime": x["anime"],
                "season": x["season"],
                "episode": x["episode"],
                "start": x["start"],
                "end": x["end"],
                "edit_start": x["edit_start"],
                "edit_end": x["edit_end"],
                "edit_duration": max(0.0, x["edit_end"] - x["edit_start"]),
                "speed": x["speed"],
                "confidence": x["confidence"],
            }
            for x in results
        ],
        "total": len(regions),
        "qa": {"match": True, "confidence": min(x["confidence"] for x in results)},
        "report": "📋 EXACT SOURCE TIMESTAMPS\n\n" + report,
        "sources": {(x["anime"].lower(), x["season"], x["episode"]) for x in results},
    }
