import asyncio
import json
import logging
import mimetypes
import re
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
                {"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{file_name}"}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
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


def _analysis_prompt(catalog: str):
    return f"""
You are a SIMPLE anime clip timestamp detector.
Watch the ENTIRE uploaded YouTube edit and identify every contiguous anime clip in it.
Do not do visual source matching, candidate searching, fingerprinting or verification.
Your only job is to tell the bot approximately where each clip comes from.

For every anime clip, give:
- anime: closest exact name from the catalog below
- season: source season number
- episode: source episode number
- source_start_hint: approximate START timestamp inside the original episode
- start_time/end_time: start/end timestamps of that clip inside the uploaded edit
- confidence: your confidence

The source_start_hint is REQUIRED whenever you can identify the episode. Never leave it null if an approximate timestamp can be estimated.
Use seconds as numbers for all timestamps. Example: 12:35 = 755.
If you are unsure by a few seconds, still give your best approximate timestamp. Do NOT refuse a scene just because the timestamp may be shifted.

Source catalog:
{catalog}

Return ONLY this JSON shape:
{{"regions":[{{"start_time":0.0,"end_time":5.0,"anime":"NARUTO","season":1,"episode":27,"source_start_hint":755.0,"confidence":0.9}}]}}
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
    logger.info("Gemini simple scene-analysis pass=1")
    data = _generate_video_prompt(name, _analysis_prompt(_catalog_text()), temperature=0.0)
    parsed = _parse_json(_text_from_response(data))
    regions = parsed.get("regions") if isinstance(parsed, dict) else None
    if not isinstance(regions, list) or not regions:
        raise RuntimeError("Gemini returned no usable anime regions")
    return parsed


def _parse_time_value(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return None
        if len(numbers) == 2:
            return numbers[0] * 60.0 + numbers[1]
        if len(numbers) == 3:
            return numbers[0] * 3600.0 + numbers[1] * 60.0 + numbers[2]
    try:
        return float(text)
    except ValueError:
        return None


def _clean_regions(data):
    regions = data.get("regions", []) if isinstance(data, dict) else []
    cleaned = []
    for item in regions:
        if not isinstance(item, dict):
            continue
        start = _parse_time_value(item.get("start_time"))
        end = _parse_time_value(item.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        anime = item.get("anime")
        if isinstance(anime, str):
            anime = anime.strip()
        for key in ("season", "episode"):
            value = item.get(key)
            if value is not None:
                try:
                    item[key] = int(value)
                except (TypeError, ValueError):
                    match = re.search(r"\d+", str(value))
                    item[key] = int(match.group()) if match else None
        item["source_start_hint"] = _parse_time_value(item.get("source_start_hint"))
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
