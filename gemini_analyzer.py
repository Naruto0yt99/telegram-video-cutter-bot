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

# Fast multimodal model first. The 3.5 Flash-Lite model is designed for
# low-latency/high-throughput work; heavier models are only fallbacks.
MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ("gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash")
API_ROOT = "https://generativelanguage.googleapis.com"
MODEL_ATTEMPTS = 1
REQUEST_TIMEOUT = httpx.Timeout(connect=20.0, read=90.0, write=120.0, pool=20.0)


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
    deadline = time.monotonic() + 45.0
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
            time.sleep(1.0)
    raise TimeoutError("Gemini video processing timed out.")


def _generate_sync(model: str, payload: dict):
    last_error = None
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = client.post(
                f"{API_ROOT}/v1beta/models/{model}:generateContent",
                headers=_headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning("Gemini request failed model=%s: %s", model, exc)
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
            logger.warning("Gemini model %s failed: %s", model, exc)
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
Watch the uploaded YouTube anime edit and list every contiguous anime clip.
Do ONLY timestamp identification. Do not search, compare, fingerprint, verify, or explain.
For each clip return the closest catalog anime, season, episode, approximate START time in the original episode, and the clip start/end inside the uploaded edit.
Use seconds as numbers. Best-effort timestamps are required. The source timestamp may be off by up to about 5 minutes; still return your best estimate rather than refusing or returning no result.
Return ONLY JSON.

Catalog: {catalog}

{{"regions":[{{"start_time":0,"end_time":5,"anime":"NARUTO","season":1,"episode":27,"source_start_hint":755,"confidence":0.9}}]}}
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

    # Retry once with a permissive prompt if the first JSON response has no regions.
    # This keeps FIND best-effort instead of failing before Telegram extraction.
    if not isinstance(regions, list) or not regions:
        logger.warning("Gemini pass=1 returned no regions; running permissive fallback")
        catalog = _catalog_text()
        fallback_prompt = f"""
Watch the uploaded video and identify the anime footage in it.
This is a BEST-EFFORT extraction task. Do not refuse because the exact anime,
season, episode, or timestamp is uncertain.

Return JSON only with this exact shape:
{{"regions":[{{"start_time":0,"end_time":5,"anime":"NARUTO","season":1,"episode":27,"source_start_hint":755,"confidence":0.5}}]}}

Rules:
- Return every obvious contiguous anime segment.
- start_time/end_time are seconds inside the uploaded edit.
- source_start_hint is the best approximate timestamp in the original episode, in seconds.
- If season/episode is uncertain, still make your best estimate; do not return empty regions.
- Approximate timestamps are acceptable and may be off by several minutes.
- Do not explain anything outside the JSON.

Catalog: {catalog}
"""
        fallback_data = _generate_video_prompt(name, fallback_prompt, temperature=0.1)
        fallback_parsed = _parse_json(_text_from_response(fallback_data))
        if isinstance(fallback_parsed, dict):
            regions = fallback_parsed.get("regions")
            if not isinstance(regions, list) or not regions:
                for key in ("clips", "scenes", "segments"):
                    candidate = fallback_parsed.get(key)
                    if isinstance(candidate, list) and candidate:
                        fallback_parsed["regions"] = candidate
                        regions = candidate
                        break
            if isinstance(regions, list) and regions:
                parsed = fallback_parsed

    if not isinstance(regions, list) or not regions:
        raise RuntimeError("Gemini returned no usable anime regions after fallback")
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
