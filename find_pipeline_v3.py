import asyncio
import json
import logging
import mimetypes
import re
import difflib
from pathlib import Path

import httpx

from config import GEMINI_API_KEY, TEMP_DIR, FFMPEG_BIN, FINGERPRINT_CHAT
from database import get_all_sources_for_episode, get_animes, get_seasons, get_episodes
from library_nav import canonical_anime
from telegram_remote import get_telegram_video_info, open_telegram_range_server
from ffmpeg_utils import run_command
from telegram_media import parse_telegram_message_link
from utils import safe_filename, unique_path

logger = logging.getLogger("find-pipeline-v3")

GEMINI_ROOT = "https://generativelanguage.googleapis.com"
# Requested model first; modern fallback keeps the pipeline usable if the legacy
# model is unavailable for the account.
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
GEMINI_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
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
            "-seekable", "1", "-multiple_requests", "1",
            "-initial_request_size", str(2 * 1024 * 1024),
            "-request_size", str(2 * 1024 * 1024),
            "-short_seek_size", str(2 * 1024 * 1024),
            "-i", server.url,
            "-vf", "fps=2.5,scale=-2:360",
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "31",
            "-movflags", "+faststart",
            proxy,
        ]
        last_error = None
        for attempt in range(3):
            try:
                await run_command(*cmd)
                if Path(proxy).exists() and Path(proxy).stat().st_size > 0:
                    break
            except Exception as exc:
                last_error = exc
                try:
                    os.remove(proxy)
                except OSError:
                    pass
                if attempt >= 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        if not Path(proxy).exists() or Path(proxy).stat().st_size == 0:
            raise last_error or RuntimeError("Telegram visual proxy empty bana.")

        path = Path(proxy)
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError("Gemini visual proxy empty bana.")

        last_error = None
        for attempt in range(3):
            try:
                file_data = await _gemini_upload_path(path)
                if not file_data.get("name"):
                    raise RuntimeError("Gemini ne visual proxy ko file ke roop me accept nahi kiya.")
                return file_data, duration, path.stat().st_size
            except Exception as exc:
                last_error = exc
                if attempt >= 2:
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


async def _gemini_upload_telegram_window(client, source_url: str, start: float, end: float, display_name: str):
    """Upload only a small candidate window to Gemini for final verification."""
    import os
    import tempfile

    server = await open_telegram_range_server(client, source_url)
    proxy = None
    try:
        fd, proxy = tempfile.mkstemp(prefix="gemini_window_", suffix=".mp4")
        os.close(fd)
        duration = max(1.0, float(end) - float(start))
        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-seekable", "1", "-multiple_requests", "1",
            "-initial_request_size", str(2 * 1024 * 1024),
            "-request_size", str(2 * 1024 * 1024),
            "-short_seek_size", str(2 * 1024 * 1024),
            "-ss", f"{max(0.0, float(start)):.3f}",
            "-i", server.url,
            "-t", f"{min(duration, 55.0):.3f}",
            "-vf", "fps=4,scale=-2:360",
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-movflags", "+faststart", proxy,
        ]
        await run_command(*cmd)
        if not Path(proxy).exists() or Path(proxy).stat().st_size <= 0:
            raise RuntimeError("Gemini candidate window empty bana.")
        file_data = await _gemini_upload_path(Path(proxy))
        if not file_data.get("name"):
            raise RuntimeError("Gemini candidate window upload failed.")
        return file_data, duration
    finally:
        await server.close()
        if proxy:
            try:
                os.remove(proxy)
            except OSError:
                pass


async def _wait_active(name: str):
    deadline = asyncio.get_running_loop().time() + 180
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

async def _generate(prompt, files, media_resolution="MEDIA_RESOLUTION_LOW"):
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
                            },
                            "media_processing": "AGENTIC",
                            "media_resolution": {"level": media_resolution},
                        }
                        for uri in uris
                    ],
                    {"text": prompt},
                ],
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": False},
            },
        }

        for model in GEMINI_MODELS:
            for attempt in range(2):
                try:
                    r = await client.post(
                        f"{GEMINI_ROOT}/v1beta/models/{model}:generateContent",
                        headers={**_headers(), "Content-Type": "application/json"},
                        json=payload,
                    )
                    if r.status_code in GEMINI_RETRYABLE_STATUS and attempt < 1:
                        retry_after = r.headers.get("retry-after")
                        try:
                            delay = min(20.0, max(2.0, float(retry_after)))
                        except (TypeError, ValueError):
                            delay = 2.0 * (attempt + 1)
                        logger.warning("Gemini retryable %s from %s; retrying in %.1fs", r.status_code, model, delay)
                        await asyncio.sleep(delay)
                        continue
                    if r.status_code >= 400:
                        body = r.text[:4000]
                        logger.error("Gemini HTTP %s model=%s body=%s", r.status_code, model, body)
                        if r.status_code not in GEMINI_RETRYABLE_STATUS:
                            raise RuntimeError(f"Gemini HTTP {r.status_code} ({model}): {body}")
                    r.raise_for_status()
                    try:
                        data = r.json()
                    except Exception as exc:
                        logger.error("Gemini returned non-JSON HTTP body model=%s body=%s", model, r.text[:4000])
                        raise RuntimeError(f"Gemini response JSON parse failed ({model}): {r.text[:1000]}") from exc
                    text_parts = []
                    for candidate in data.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            if part.get("text"):
                                text_parts.append(part["text"])
                    raw_text = "\n".join(text_parts).strip()
                    if not raw_text:
                        candidates_meta = []
                        for candidate in data.get("candidates", []):
                            candidates_meta.append({
                                "finishReason": candidate.get("finishReason"),
                                "safetyRatings": candidate.get("safetyRatings"),
                                "citationMetadata": candidate.get("citationMetadata"),
                            })
                        logger.warning(
                            "Gemini returned empty text model=%s attempt=%s candidates=%s promptFeedback=%s",
                            model, attempt + 1, candidates_meta, data.get("promptFeedback"),
                        )
                        # Some Gemini video calls return a valid HTTP response with no
                        # text when structured-output/thinking settings are combined
                        # with agentic video processing. Retry once with a plain text
                        # generation config before abandoning this model.
                        if attempt == 0:
                            payload_plain = {
                                "contents": payload["contents"],
                                "generationConfig": {"maxOutputTokens": 4096},
                            }
                            try:
                                fallback = await client.post(
                                    f"{GEMINI_ROOT}/v1beta/models/{model}:generateContent",
                                    headers={**_headers(), "Content-Type": "application/json"},
                                    json=payload_plain,
                                )
                                if fallback.status_code < 400:
                                    fallback_data = fallback.json()
                                    fallback_parts = []
                                    for candidate in fallback_data.get("candidates", []):
                                        for part in candidate.get("content", {}).get("parts", []):
                                            if part.get("text"):
                                                fallback_parts.append(part["text"])
                                    fallback_text = "\n".join(fallback_parts).strip()
                                    if fallback_text:
                                        try:
                                            return _json(fallback_text)
                                        except Exception:
                                            logger.warning(
                                                "Gemini plain fallback produced non-JSON text model=%s text=%s",
                                                model, fallback_text[:2000],
                                            )
                                else:
                                    logger.warning(
                                        "Gemini plain fallback HTTP %s model=%s body=%s",
                                        fallback.status_code, model, fallback.text[:2000],
                                    )
                            except Exception as fallback_exc:
                                logger.warning(
                                    "Gemini plain fallback failed model=%s: %s",
                                    model, fallback_exc,
                                )
                            await asyncio.sleep(1.5)
                            continue
                        last = RuntimeError(f"Gemini returned empty text ({model})")
                        break
                    try:
                        return _json(raw_text)
                    except Exception as exc:
                        logger.error(
                            "Gemini returned invalid JSON model=%s attempt=%s text=%s",
                            model, attempt + 1, raw_text[:4000],
                        )
                        if attempt < 1:
                            await asyncio.sleep(1.5)
                            continue
                        last = RuntimeError(f"Gemini returned invalid JSON ({model}): {raw_text[:1000]}")
                        break
                except Exception as exc:
                    last = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in GEMINI_RETRYABLE_STATUS and attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    logger.warning("Gemini model failed: %s: %s", model, exc)
                    break
    raise last or RuntimeError("Gemini request failed.")


def _to_seconds(value):
    """Parse Gemini timestamps such as 12.345, 01:23.450 or 00:01:23."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return float(parts[0]) * 60.0 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
    except ValueError:
        return None
    return None


def _normalize_exact_matches(data, expected_count=0):
    """Normalize Gemini's source timestamp matches while preserving scene order."""
    if not isinstance(data, dict):
        return []
    raw = data.get("matches") or data.get("regions") or data.get("scenes") or data.get("segments")
    if isinstance(raw, dict):
        raw = raw.get("matches") or raw.get("regions") or raw.get("scenes") or raw.get("segments")
    if not isinstance(raw, list):
        return []
    out = []
    for pos, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        start = _to_seconds(item.get("source_start", item.get("start_time", item.get("start"))))
        end = _to_seconds(item.get("source_end", item.get("end_time", item.get("end"))))
        if start is None or end is None or end <= start:
            continue
        scene_index = item.get("scene_index", item.get("index", pos + 1))
        try:
            scene_index = int(scene_index)
        except (TypeError, ValueError):
            scene_index = pos + 1
        out.append({
            "scene_index": scene_index,
            "source_start": float(start),
            "source_end": float(end),
            "confidence": float(item.get("confidence", 0) or 0) if str(item.get("confidence", "")).replace(".", "", 1).isdigit() else 0.0,
            "reason": str(item.get("reason") or item.get("description") or "").strip(),
        })
    out.sort(key=lambda x: x["scene_index"])
    return out


async def _gemini_exact_episode_match(episode_file_name, short_file_name, regions):
    """Use the full episode + Short together for coarse-to-fine source timing."""
    scene_lines = []
    for i, r in enumerate(regions, 1):
        desc = str(r.get("description") or "").strip()
        chars = ", ".join(str(x) for x in (r.get("characters") or [])[:8])
        landmarks = ", ".join(str(x) for x in (r.get("landmarks") or [])[:8])
        scene_lines.append(
            f"Scene {i}: Short {float(r.get('start_time', 0)):.3f}-{float(r.get('end_time', 0)):.3f}s; "
            f"description={desc}; characters={chars}; landmarks={landmarks}"
        )
    prompt = """You are matching edited anime Short footage to the ORIGINAL EPISODE.
VIDEO 1 is the Short. VIDEO 2 is the complete source episode.

For every listed Short scene, find the SAME visual event in VIDEO 2. Do not use opening,
ending, recap, preview, or unrelated visually similar shots unless the Short actually shows it.
Use character identity, exact pose/action sequence, background, camera movement, cuts and
chronological context. The scene must match the visual evidence, not merely the anime title.

IMPORTANT TIMESTAMP RULES:
- Return timestamps in VIDEO 2, not VIDEO 1.
- Give the first frame/time where the matching continuous scene begins and the last frame/time
  where it ends, including the full action visible in the Short.
- Timestamps may contain milliseconds.
- Do not round to whole seconds.
- If the Short uses a speed change, compare the visual action itself rather than assuming equal duration.
- Treat the Short scene description, characters, landmarks, pose/action sequence and chronological context
  as evidence. A title/character match alone is NOT enough.
- Never choose the first occurrence of a character, the opening, or a visually similar shot merely because
  it looks plausible.
- Prefer the occurrence whose complete action sequence matches the Short from beginning to end.
- Source matches for different Short scenes must not all collapse onto the same unrelated opening shot.
- Never guess from a source_start_hint when the episode video contradicts it.
- If a scene is not actually present, omit it rather than inventing a timestamp.

Return JSON only:
{"matches":[{"scene_index":1,"source_start":123.456,"source_end":130.789,
"confidence":0.98,"reason":"specific matching visual sequence"}]}

Scenes to locate:
""" + "\n".join(scene_lines)
    return await _generate(prompt, [short_file_name, episode_file_name], media_resolution="MEDIA_RESOLUTION_MEDIUM")


async def _gemini_refine_match(short_file_name, window_file_name, scene, window_start, window_duration):
    """Refine one coarse hit using timestamps relative to the uploaded window.

    Gemini cannot reliably know the absolute episode clock from a freshly-created
    clipped file. The previous implementation asked it to return full-episode
    timestamps anyway, which allowed it to hallucinate values such as 19s or
    1353s even when the actual window was around 3:45. We now force window-relative
    timestamps and convert them to the episode clock in Python.
    """
    short_start = float(scene.get("start_time", 0) or 0)
    short_end = float(scene.get("end_time", 0) or 0)
    duration = max(0.2, short_end - short_start)
    prompt = f"""VIDEO 1 is the ORIGINAL EDITED SHORT. VIDEO 2 is ONLY a small source-episode
window. VIDEO 2 starts at 0.000 seconds because it was clipped from the episode.

The original episode window corresponds to full-episode time {window_start:.3f}s through
{window_start + window_duration:.3f}s.

Focus ONLY on the target Short scene {short_start:.3f}-{short_end:.3f}s from VIDEO 1.
Find the SAME continuous visual event in VIDEO 2. Do not match a visually similar opening,
ending, recap, title card, or unrelated shot.

CRITICAL TIMESTAMP RULE:
Return timestamps RELATIVE TO VIDEO 2, not the full episode clock.
Example: if the matching action is 3.2 seconds after the beginning of VIDEO 2, return 3.200,
not the absolute episode timestamp. Python will add the window offset afterward.

Boundary requirements:
- start at the first visible frame of the matching action
- end at the last visible frame of the matching action
- use decimal seconds/milliseconds
- both values MUST be between 0.000 and {window_duration:.3f}
- do not invent an absolute timestamp outside VIDEO 2
- preserve the same chronological visual action even if the Short changed speed
- if the exact matching action is not visible in VIDEO 2, return {{"match":false}}

Return JSON only:
{{"match":true,"window_start":3.200,"window_end":12.450,"confidence":0.99,
"reason":"specific visual evidence proving this is the same action"}}
"""
    data = await _generate(prompt, [short_file_name, window_file_name], media_resolution="MEDIA_RESOLUTION_MEDIUM")
    if not isinstance(data, dict) or data.get("match") is False:
        return None
    start = _to_seconds(data.get("window_start", data.get("source_start", data.get("start_time", data.get("start")))))
    end = _to_seconds(data.get("window_end", data.get("source_end", data.get("end_time", data.get("end")))))
    if start is None or end is None or end <= start:
        return None
    start = max(0.0, min(float(start), float(window_duration)))
    end = max(0.0, min(float(end), float(window_duration)))
    if end <= start:
        return None
    return {
        "source_start": float(window_start) + start,
        "source_end": float(window_start) + end,
        "confidence": float(data.get("confidence", 0) or 0) if str(data.get("confidence", "")).replace(".", "", 1).isdigit() else 0.0,
        "reason": str(data.get("reason") or data.get("description") or "").strip(),
    }


def _normalize_regions(data):
    """Accept the strict regions schema plus common Gemini JSON variants."""
    if not isinstance(data, dict):
        return []
    raw = data.get("regions") or data.get("scenes") or data.get("shots") or data.get("segments")
    if isinstance(raw, dict):
        raw = raw.get("regions") or raw.get("scenes") or raw.get("shots") or raw.get("segments")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_time", item.get("start"))
        end = item.get("end_time", item.get("end"))
        if start is None or end is None:
            continue
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        normalized = dict(item)
        normalized["start_time"] = start
        normalized["end_time"] = end
        normalized["anime"] = str(item.get("anime") or item.get("title") or item.get("anime_title") or "").strip()
        out.append(normalized)
    return out


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


def _library_anime_match(value):
    """Resolve Gemini's title against the actual DB without brittle exact matching.

    Gemini may return franchise subtitles, punctuation variants, romanization,
    or extra words such as "TV Series". The library stores the canonical DB
    title, so harmless naming differences must never kill FIND.
    """
    raw = str(value or "").strip()
    if not raw:
        return None

    canonical = canonical_anime(raw)
    try:
        db_names = [str(x).strip() for x in get_animes() if str(x).strip()]
    except Exception:
        db_names = []

    if not db_names:
        return canonical

    # Keep the real DB spelling as the value used by database.py.
    candidates = []
    for db_name in db_names:
        canon = canonical_anime(db_name) or db_name
        candidates.append((db_name, canon))
    candidates = list(dict.fromkeys(candidates))

    def norm(x):
        return re.sub(r"[^a-z0-9]+", "", str(x).casefold())

    def tokens(x):
        return set(re.findall(r"[a-z0-9]+", str(x).casefold()))

    raw_norm = norm(raw)
    raw_tokens = tokens(raw)
    canon_norm = norm(canonical) if canonical else ""

    # 1) Canonical alias match.
    if canon_norm:
        for db_name, db_canon in candidates:
            if norm(db_canon) == canon_norm:
                return db_name

    # 2) Exact normalized title.
    for db_name, _ in candidates:
        if norm(db_name) == raw_norm:
            return db_name

    # 3) Token/substring matching. This handles titles such as:
    # "Re:ZERO -Starting Life in Another World-" -> "Re:Zero"
    # "Death Note TV series" -> "Death Note"
    scored = []
    for db_name, db_canon in candidates:
        db_norm = norm(db_name)
        db_tokens = tokens(db_name)
        score = difflib.SequenceMatcher(None, raw_norm, db_norm).ratio()

        common = len(raw_tokens & db_tokens)
        if common:
            score += min(0.35, 0.12 * common)

        if raw_norm and (raw_norm in db_norm or db_norm in raw_norm):
            score += 0.35

        if canon_norm and (canon_norm in db_norm or db_norm in canon_norm):
            score += 0.25

        # Strong protection against accidental matches on a single tiny word.
        if raw_tokens and db_tokens and common >= min(2, len(raw_tokens)):
            score += 0.20

        scored.append((score, db_name))

    scored.sort(reverse=True, key=lambda x: x[0])
    if scored:
        best_score, best_name = scored[0]
        # A short title needs less similarity when it is a clear substring;
        # otherwise require meaningful overlap.
        if best_score >= 0.62 or (raw_norm and norm(best_name) in raw_norm):
            logger.info("Library anime resolver: %r -> %r (score %.3f)", raw, best_name, best_score)
            return best_name

    # Do not fabricate an anime name. Returning canonical is still useful when
    # the DB is empty or another resolver adds the title later.
    return canonical


def _episode_candidates(anime, season, episode):
    try:
        seasons = [str(x) for x in get_seasons(anime)]
    except Exception:
        seasons = []
    if not seasons:
        return [(anime, season, episode)]
    requested_season = str(season) if season is not None else None
    selected_season = requested_season if requested_season in seasons else None
    if selected_season is None and requested_season:
        m = re.search(r"\d+", requested_season)
        if m:
            wanted = int(m.group())
            numeric_seasons = []
            for x in seasons:
                mx = re.search(r"\d+", x)
                numeric_seasons.append((abs(int(mx.group()) - wanted), x) if mx else (10**9, x))
            selected_season = min(numeric_seasons)[1]
    if selected_season is None:
        selected_season = seasons[0]
    try:
        episodes = [str(x) for x in get_episodes(anime, selected_season)]
    except Exception:
        episodes = []
    if not episodes:
        return [(anime, selected_season, episode)]
    if episode is None:
        # No episode from Gemini is not an error. Give the fingerprint stage a
        # broad but bounded candidate set instead of arbitrarily taking E1-E8.
        ordered = sorted(episodes, key=lambda x: int(x) if x.isdigit() else 10**9)
        return [(anime, selected_season, e) for e in ordered[:80]]
    wanted = str(episode)
    nums = sorted(episodes, key=lambda x: int(x) if x.isdigit() else 10**9)
    if wanted in nums:
        pos = nums.index(wanted)
        ordered = nums[max(0, pos - 2):pos + 3]
    else:
        try:
            w = int(wanted)
            ordered = sorted(nums, key=lambda x: abs(int(x) - w))[:5]
        except Exception:
            ordered = nums[:5]
    return [(anime, selected_season, e) for e in dict.fromkeys(ordered)]


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
        media_resolution="MEDIA_RESOLUTION_MEDIUM",
    )
    regions = _normalize_regions(analysis)
    if not regions:
        logger.warning("Gemini first-pass returned no normalized regions: %s", str(analysis)[:2000])
        retry_analysis = await _generate(
            """Re-analyze VIDEO 1. Identify the anime footage in the Short by VISUAL CONTENT only.
Return every distinct contiguous shot as a list. Do not require certainty about season, episode,
or timestamp. If the exact anime is uncertain, give your best title guess and leave season/episode
null. Never return an empty list when visible video frames contain anime footage.

Return JSON only:
{"regions":[{"start_time":0,"end_time":3,"anime":"best title guess","season":null,"episode":null,
"description":"specific visible characters, setting, action, colors and camera movement",
"characters":[],"location":"","landmarks":[]}]}

The important requirement is to describe what is visibly present, not to explain uncertainty.""",
            [edit_file["name"]],
            media_resolution="MEDIA_RESOLUTION_MEDIUM",
        )
        regions = _normalize_regions(retry_analysis)
    if not regions:
        raise RuntimeError("Gemini ko Short me koi usable scene nahi mila.")

    await progress(f"🎯 FIND — 25%\n\n📚 {len(regions)} scenes identify ho gaye.\n🔎 Telegram library se exact source episode locate ho raha hai...")

    # Group by episode so an episode is streamed to Gemini only once even if
    # several Short scenes came from it.
    groups = {}
    for r in regions:
        anime = _library_anime_match(r.get("anime"))
        if not anime:
            continue
        season = r.get("season")
        episode = r.get("episode")
        try:
            season = int(season) if season is not None else None
        except (TypeError, ValueError):
            season = None
        try:
            episode = int(episode) if episode is not None else None
        except (TypeError, ValueError):
            episode = None
        for cand_anime, cand_season, cand_episode in _episode_candidates(anime, season, episode):
            try:
                cand_season_i = int(cand_season)
                cand_episode_i = int(cand_episode)
            except (TypeError, ValueError):
                continue
            low, high, sources = _source_pair(cand_anime, cand_season_i, cand_episode_i)
            if low and high:
                groups.setdefault((cand_anime, cand_season_i, cand_episode_i), (low, high, sources))
    if not groups:
        # Resolver/episode uncertainty is recoverable. Only report a hard
        # failure after all normalized library candidates were exhausted.
        seen = sorted({str(r.get("anime") or "").strip() for r in regions if r.get("anime")})
        raise RuntimeError(
            "FIND source candidates nahi mile. Gemini titles=%s; "
            "library resolver ne available Telegram sources me koi usable episode nahi paya."
            % (", ".join(seen[:8]) or "unknown")
        )

    # Gemini episode matching: no fingerprint/index/visual matcher is used here.
    # Upload each identified Telegram episode once as a temporary low-resolution proxy, then
    # compare the Short against that complete episode. A second Gemini pass on a small source
    # window refines each boundary before FFmpeg cuts the original high-quality Telegram source.
    episode_files = {}
    exact_matches = {}

    for group_key, (_low, high, _sources) in groups.items():
        anime_name, season_no, episode_no = group_key
        source_url = high
        try:
            await progress(
                f"🎯 FIND — 40%\\n\\n"
                f"🧠 Gemini ko {anime_name} S{season_no} E{episode_no} ka episode video diya ja raha hai...\\n"
                "⏳ Episode ko temporary analysis copy me prepare kiya ja raha hai."
            )
            file_data, episode_duration, proxy_size = await _gemini_upload_telegram(
                telegram_client,
                source_url,
                f"{safe_filename(anime_name)}_S{season_no}_E{episode_no}.mp4",
            )
            await _wait_active(file_data["name"])
            episode_files[group_key] = (file_data["name"], float(episode_duration), source_url)
            logger.info(
                "Gemini episode proxy ready %s S%s E%s duration=%.3fs size=%s",
                anime_name, season_no, episode_no, episode_duration, proxy_size,
            )
        except Exception as exc:
            logger.exception("Gemini episode upload failed for %s", group_key)

    await progress(
        "🎯 FIND — 55%\\n\\n"
        "🧠 Gemini complete-episode visual matching chal rahi hai...\\n"
        "🎯 Fingerprint matching intentionally disabled."
    )

    for group_key, (episode_file_name, episode_duration, source_url) in episode_files.items():
        anime_name, season_no, episode_no = group_key
        group_regions = []
        for idx, region in enumerate(regions, 1):
            resolved = _library_anime_match(region.get("anime"))
            try:
                r_season = int(region.get("season")) if region.get("season") is not None else None
            except (TypeError, ValueError):
                r_season = None
            try:
                r_episode = int(region.get("episode")) if region.get("episode") is not None else None
            except (TypeError, ValueError):
                r_episode = None
            if (str(resolved).casefold() == str(anime_name).casefold()
                    and (r_season is None or r_season == season_no)
                    and (r_episode is None or r_episode == episode_no)):
                copy = dict(region)
                copy["scene_index"] = idx
                group_regions.append(copy)

        if not group_regions:
            continue
        try:
            coarse = await _gemini_exact_episode_match(
                episode_file_name, edit_file["name"], group_regions
            )
            matches = _normalize_exact_matches(coarse, len(group_regions))
        except Exception as exc:
            logger.exception("Gemini complete-episode match failed for %s", group_key)
            matches = []

        for match in matches:
            scene = next((r for r in group_regions if r["scene_index"] == match["scene_index"]), None)
            if scene is None:
                continue
            # Refine in a narrow window around the coarse location. The window is temporary and
            # is deleted immediately; the complete episode is never stored permanently on Termux.
            coarse_start = max(0.0, min(float(match["source_start"]), episode_duration))
            coarse_end = max(coarse_start + 0.2, min(float(match["source_end"]), episode_duration))
            pad_before = 8.0
            pad_after = 8.0
            window_start = max(0.0, coarse_start - pad_before)
            window_end = min(episode_duration, coarse_end + pad_after)
            try:
                window_file, _requested_duration = await _gemini_upload_telegram_window(
                    telegram_client,
                    source_url,
                    window_start,
                    window_end,
                    f"refine_S{season_no}_E{episode_no}_{scene['scene_index']}.mp4",
                )
                await _wait_active(window_file["name"])
                refined = await _gemini_refine_match(
                    edit_file["name"],
                    window_file["name"],
                    scene,
                    window_start,
                    min(window_end - window_start, 55.0),
                )
                if refined:
                    match.update(refined)
                    # The refine prompt already requests full-episode timestamps.
                    logger.info(
                        "Gemini refined scene %s %s S%s E%s: %.3f-%.3f confidence=%.3f",
                        scene["scene_index"], anime_name, season_no, episode_no,
                        match["source_start"], match["source_end"], match.get("confidence", 0.0),
                    )
            except Exception as exc:
                # Keep the coarse Gemini result if the narrow refinement fails.
                logger.warning("Gemini narrow refinement failed for scene %s: %s", scene["scene_index"], exc)

            start_time = max(0.0, min(float(match["source_start"]), episode_duration))
            end_time = max(start_time + 0.05, min(float(match["source_end"]), episode_duration))
            if end_time <= start_time:
                continue
            results.append({
                "index": scene["scene_index"],
                "anime": anime_name,
                "season": season_no,
                "episode": episode_no,
                "start": start_time,
                "end": end_time,
                "edit_start": float(scene.get("start_time", 0) or 0),
                "edit_end": float(scene.get("end_time", 0) or 0),
                "speed": 1.0,
                "confidence": max(0.0, min(1.0, float(match.get("confidence", 0) or 0))),
                "high": source_url,
                "match_reason": match.get("reason", ""),
            })

    if not results:

        raise RuntimeError(
            "FIND me saved fingerprint match nahi mila. Visual matcher aur Gemini final verification intentionally skip kiye gaye hain."
        )

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
