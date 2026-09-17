import asyncio
import json
import logging
import mimetypes
import time
from pathlib import Path

import httpx

from config import GEMINI_API_KEY

logger = logging.getLogger("gemini-analyzer")

MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.6-flash")
API_ROOT = "https://generativelanguage.googleapis.com"

# A transient Gemini 503 should not make a FIND job spend several minutes
# retrying the same model. One short retry per model is enough; the next model
# is then tried immediately.
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
            headers={**_headers(), "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": mime},
        )
        start.raise_for_status()
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError("Gemini upload URL nahi mila.")

        with path.open("rb") as fh:
            response = client.post(
                upload_url,
                headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
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
                    logger.warning("Gemini transient error model=%s status=%s attempt=%s/%s", model, response.status_code, attempt, MODEL_ATTEMPTS)
                    if attempt < MODEL_ATTEMPTS:
                        time.sleep(RETRY_DELAYS[attempt - 1])
                        continue
                    break
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                if attempt < MODEL_ATTEMPTS:
                    logger.warning("Gemini request error model=%s attempt=%s/%s: %s", model, attempt, MODEL_ATTEMPTS, exc)
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
            return _generate_sync(model, payload)
        except Exception as exc:
            last_error = exc
            logger.warning("Gemini model %s exhausted: %s", model, exc)
    raise last_error or RuntimeError("All Gemini models failed")


def _analyze_uploaded(file_name: str, prompt: str):
    payload = {
        "contents": [{"role": "user", "parts": [{"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{file_name}"}}, {"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    return _generate_with_fallback(payload)


def analyze_video(path: Path):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    logger.info("Gemini upload: %s (%0.1f MB)", path.name, path.stat().st_size / 1024 / 1024)
    uploaded = _upload_file(path)
    name = uploaded.get("name")
    if not name:
        raise RuntimeError("Gemini file upload failed")
    _wait_file_active(name)

    prompt = """
Analyze the entire uploaded anime edit and return ONLY valid JSON.
Split the edit into every real source-scene region. Ignore intros, subtitles-only moments, black frames, effects-only transitions, and duplicate frames.
For each usable region identify anime, season if known, episode if known, approximate source_start_hint in seconds, and confidence.
Account for speed changes, reverse playback, zoom/crop, mirror, color changes, overlays and transitions. Use concrete visual landmarks when possible. If uncertain, use null instead of inventing an episode.
Return: {"regions":[{"start_time":0,"end_time":0,"anime":"...","season":1,"episode":27,"source_start_hint":123.0,"confidence":0.0,"reason":"..."}]}
For Naruto Forest of Death entry/setup landmarks, remember the original Naruto episode 27 boundary: the Chunin Exam Stage 2 / Forest of Death setup immediately before or at the forest gates is episode 27; episode 28 continues the subsequent early-forest action.
"""
    data = _analyze_uploaded(name, prompt)
    text = _text_from_response(data)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise RuntimeError("Gemini returned invalid JSON")


def _clean_regions(data):
    if isinstance(data, dict):
        regions = data.get("regions", [])
    else:
        regions = []
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
        cleaned.append({**item, "start_time": start, "end_time": end})
    return cleaned


def verify_candidate_window(candidate_path: Path, target_path: Path, context: dict | None = None):
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
Compare the TARGET edit against the CANDIDATE SOURCE WINDOW.
Return ONLY JSON: {{"match":true/false,"confidence":0.0,"start_time":0.0,"end_time":0.0,"reason":"..."}}
Find the exact continuous source interval in the candidate that visually corresponds to the target. Ignore subtitles, logos, crops, mirrors, speed changes, color grading and overlays. start_time/end_time must be relative to the candidate video.
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
    text = _text_from_response(data)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise RuntimeError("Gemini verification returned invalid JSON")
