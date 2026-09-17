import asyncio
import json
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from config import GEMINI_API_KEY
from database import get_animes

logger = logging.getLogger("gemini-analyzer")

MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite")
API_ROOT = "https://generativelanguage.googleapis.com"
MODEL_ATTEMPTS = 2
RETRY_DELAYS = (2.0, 4.0)
REQUEST_TIMEOUT = httpx.Timeout(connect=20.0, read=90.0, write=90.0, pool=20.0)


def _headers():
    return {"x-goog-api-key": GEMINI_API_KEY}


def _upload_file(path: Path):
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    size = path.stat().st_size
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        start = client.post(
            f"{API_ROOT}/upload/v1beta/files",
            headers={
                **_headers(),
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime,
            },
        )
        start.raise_for_status()
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError("Gemini upload URL nahi mila.")
        with path.open("rb") as fh:
            response = client.post(
                upload_url,
                headers={
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                },
                content=fh,
            )
        response.raise_for_status()
        return response.json().get("file", {})


def _wait_file_active(name: str):
    deadline = time.monotonic() + 60.0
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        while time.monotonic() < deadline:
            response = client.get(f"{API_ROOT}/v1beta/{name}", headers=_headers())
            response.raise_for_status()
            data = response.json()
            state = data.get("state") or data.get("file", {}).get("state")
            if state in (None, "ACTIVE"):
                return data
            if state == "FAILED":
                raise RuntimeError("Gemini video processing failed.")
            time.sleep(1.5)
    raise TimeoutError("Gemini video processing timed out.")


def _generate_sync(model: str, payload: dict):
    last_error = None
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for attempt in range(1, MODEL_ATTEMPTS + 1):
            try:
                response = client.post(
                    f"{API_ROOT}/v1beta/models/{model}:generateContent",
                    headers=_headers(),
                    json=payload,
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = RuntimeError(f"Gemini HTTP {response.status_code}")
                    logger.warning(
                        "Gemini transient error model=%s status=%s attempt=%s/%s",
                        model, response.status_code, attempt, MODEL_ATTEMPTS,
                    )
                    if attempt < MODEL_ATTEMPTS:
                        time.sleep(RETRY_DELAYS[attempt - 1])
                        continue
                    break
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                if attempt < MODEL_ATTEMPTS:
                    logger.warning(
                        "Gemini request error model=%s attempt=%s/%s: %s",
                        model, attempt, MODEL_ATTEMPTS, exc,
                    )
                    time.sleep(RETRY_DELAYS[attempt - 1])
    raise last_error or RuntimeError("Gemini request failed")


def _text_from_response(data: dict) -> str:
    parts = []
    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            text = part.get("text")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _generate_with_fallback(payload: dict):
    last_error = None
    for model in (MODEL, *FALLBACK_MODELS):
        try:
            logger.info("Gemini generate model=%s", model)
            return _generate_sync(model, payload)
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini model %s exhausted: %s", model, exc)
    raise last_error or RuntimeError("All Gemini models failed")


def _generate_video_prompt(file_name: str, prompt: str, temperature=0.0):
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "file_data": {
                        "mime_type": "video/mp4",
                        "file_uri": f"{API_ROOT}/v1beta/{file_name}",
                    }
                },
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    return _generate_with_fallback(payload)


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        raise ValueError("Gemini returned empty text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _catalog_text():
    try:
        names = get_animes()
    except Exception:
        names = []
    return ", ".join(str(name) for name in names[:80]) or "No catalog available"


def _analysis_prompt(catalog: str, compact=False):
    detail = "" if compact else "\nUse visual landmarks, character identities, costumes, locations, arc events and known episode boundaries."
    return f"""
You are the scene-identification engine for an anime clip retrieval system.
Analyze the ENTIRE uploaded edit carefully. Do not guess from one frame.
{detail}

The source library currently contains these anime names:
{catalog}

For every contiguous region that contains actual anime footage, return one region.
Do NOT discard a region merely because episode or timestamp is uncertain. The bot can search the source catalog when those fields are unknown.
Anime should be the closest name from the catalog whenever possible. Season and episode may be null.
source_start_hint is only a search hint and may be null.
start_time/end_time are timestamps in THIS uploaded edit.
Handle speed ramps, reverse playback, mirror, crop, zoom, color grading, overlays and transitions.
Separate genuinely different source scenes even when the same anime is used.

Return ONLY JSON in this exact shape:
{{"regions":[{{"start_time":0.0,"end_time":5.0,"anime":"NARUTO","season":1,"episode":27,"source_start_hint":123.0,"confidence":0.9,"landmarks":["..."],"arc":"...","reason":"..."}}]}}

If the anime is recognizable but episode is uncertain, still return the region with episode=null.
If a short transition contains usable source frames, include it with a conservative time range.
For Naruto Forest of Death entry/setup landmarks, remember that original Naruto episode 27 covers the Chunin Exam Stage 2 / Forest of Death setup immediately before or at the forest gates; episode 28 continues the subsequent early-forest action.
"""


def _analyze_video_sync(path: Path):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")
    logger.info("Gemini upload: %s (%0.1f MB)", path.name, path.stat().st_size / 1024 / 1024)
    uploaded = _upload_file(path)
    name = uploaded.get("name")
    if not name:
        raise RuntimeError("Gemini file upload failed")
    _wait_file_active(name)

    catalog = _catalog_text()
    prompts = [
        _analysis_prompt(catalog, compact=False),
        _analysis_prompt(catalog, compact=True),
    ]

    last_error = None
    for index, prompt in enumerate(prompts, 1):
        try:
            logger.info("Gemini scene-analysis pass=%s", index)
            data = _generate_video_prompt(name, prompt, temperature=0.0)
            text = _text_from_response(data)
            parsed = _parse_json(text)
            regions = parsed.get("regions") if isinstance(parsed, dict) else None
            if isinstance(regions, list) and regions:
                return parsed
            logger.warning("Gemini scene-analysis pass=%s returned no regions", index)
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini scene-analysis pass=%s failed: %s", index, exc)

    raise last_error or RuntimeError("Gemini returned no usable anime regions")


def _clean_regions(data):
    regions = data.get("regions", []) if isinstance(data, dict) else []
    cleaned = []
    for item in regions:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_time", 0))
            end = float(item.get("end_time", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        anime = item.get("anime")
        if isinstance(anime, str):
            anime = anime.strip()
        season = item.get("season")
        episode = item.get("episode")
        for key in ("season", "episode"):
            value = item.get(key)
            if value is not None:
                try:
                    item[key] = int(value)
                except (TypeError, ValueError):
                    item[key] = None
        hint = item.get("source_start_hint")
        if hint is not None:
            try:
                item["source_start_hint"] = float(hint)
            except (TypeError, ValueError):
                item["source_start_hint"] = None
        cleaned.append({
            **item,
            "anime": anime,
            "start_time": start,
            "end_time": end,
            "confidence": float(item.get("confidence", 0.0) or 0.0),
        })
    cleaned.sort(key=lambda x: x["start_time"])
    return cleaned


async def analyze_video(path: Path):
    raw = await asyncio.to_thread(_analyze_video_sync, path)
    return _clean_regions(raw)


def _verify_candidate_window_sync(candidate_path: Path, target_path: Path, context: dict | None = None):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")
    candidate = _upload_file(candidate_path)
    target = _upload_file(target_path)
    candidate_name = candidate.get("name")
    target_name = target.get("name")
    if not candidate_name or not target_name:
        raise RuntimeError("Gemini candidate upload failed")
    _wait_file_active(candidate_name)
    _wait_file_active(target_name)
    context_text = json.dumps(context or {}, ensure_ascii=False)
    prompt = f"""
You are the FINAL visual verifier.
TARGET is an edited anime clip. CANDIDATE is a source-video window.
Determine whether the candidate contains the same underlying anime footage as the target.
Ignore subtitles, logos, crop, zoom, mirror, speed changes, color grading, overlays and compression differences.
Compare multiple moments across the whole target, not just one frame.
If matched, return the tightest continuous candidate interval containing all target footage.
The returned timestamps MUST be relative to the candidate window.
Never say match=true merely because the anime/characters are similar.

Return ONLY JSON:
{{"match":true,"confidence":0.95,"start_time":1.2,"end_time":8.7,"reason":"..."}}
Context: {context_text}
"""
    payload = {
        "contents": [{"role": "user", "parts": [
            {"text": "CANDIDATE SOURCE WINDOW:"},
            {"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{candidate_name}"}},
            {"text": "TARGET EDIT:"},
            {"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{target_name}"}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    data = _generate_with_fallback(payload)
    return _parse_json(_text_from_response(data))


async def verify_candidate_window(candidate_path: Path = None, target_path: Path = None, context: dict | None = None, **kwargs):
    candidate_path = candidate_path or kwargs.get("candidate_video_path")
    target_path = target_path or kwargs.get("target_video_path")
    if candidate_path is None or target_path is None:
        raise ValueError("Candidate and target video paths are required")
    return await asyncio.to_thread(
        _verify_candidate_window_sync,
        Path(candidate_path),
        Path(target_path),
        context,
    )
