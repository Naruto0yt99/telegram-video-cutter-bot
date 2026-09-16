import asyncio
import json
import logging
import mimetypes
import re
import time
from pathlib import Path

import httpx

from config import GEMINI_API_KEY

logger = logging.getLogger("gemini-analyzer")

MODEL = "gemini-3.8-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"


def _require_key():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing.")


def _mime_type(path):
    return mimetypes.guess_type(str(path))[0] or "video/mp4"


def _upload_file_sync(path):
    _require_key()
    path = Path(path)
    size = path.stat().st_size
    mime = _mime_type(path)
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size),
        "X-Goog-Upload-Header-Content-Type": mime,
        "Content-Type": "application/json",
    }
    payload = {"file": {"display_name": path.name}}
    with httpx.Client(timeout=None, follow_redirects=True) as client:
        response = client.post(UPLOAD_URL, headers=headers, json=payload)
        response.raise_for_status()
        upload_url = response.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError("Gemini upload URL missing in response.")
        with path.open("rb") as fh:
            upload_response = client.post(
                upload_url,
                headers={
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                },
                content=fh,
            )
        upload_response.raise_for_status()
        data = upload_response.json().get("file")
        if not data or not data.get("name") or not data.get("uri"):
            raise RuntimeError("Gemini upload response missing file metadata.")
        return data


def _wait_until_active_sync(file_name):
    _require_key()
    with httpx.Client(timeout=60.0) as client:
        for _ in range(120):
            response = client.get(
                f"{BASE_URL}/{file_name}",
                headers={"x-goog-api-key": GEMINI_API_KEY},
            )
            response.raise_for_status()
            data = response.json()
            state = ((data.get("state") or {}).get("name"))
            if state == "ACTIVE":
                return data
            if state == "FAILED":
                raise RuntimeError("Gemini video processing failed.")
            time.sleep(2)
    raise TimeoutError("Gemini video processing timed out.")


def _generate_sync(file_data, prompt):
    _require_key()
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"file_data": {
                    "mime_type": file_data.get("mimeType") or file_data.get("mime_type") or "video/mp4",
                    "file_uri": file_data["uri"],
                }},
            ]
        }]
    }
    with httpx.Client(timeout=None) as client:
        response = client.post(
            f"{BASE_URL}/models/{MODEL}:generateContent",
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    text = "\n".join(str(part.get("text", "")) for part in parts if part.get("text"))
    if not text:
        raise RuntimeError(f"Gemini returned no text: {data}")
    return text


def _delete_file_sync(file_name):
    try:
        _require_key()
        with httpx.Client(timeout=30.0) as client:
            client.delete(
                f"{BASE_URL}/{file_name}",
                headers={"x-goog-api-key": GEMINI_API_KEY},
            )
    except Exception:
        logger.debug("Gemini file cleanup failed", exc_info=True)


def extract_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Gemini response me JSON nahi mila.")
    return json.loads(match.group(0))


def _clean_segments(data):
    segments = data.get("segments", []) if isinstance(data, dict) else []
    clean = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item["start_time"]))
            end = float(item["end_time"])
        except Exception:
            continue
        if end <= start:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except Exception:
            confidence = 0.0
        clean.append({
            "start_time": start,
            "end_time": end,
            "anime": str(item.get("anime") or "").strip(),
            "season": item.get("season"),
            "episode": item.get("episode"),
            "confidence": confidence,
            "source_start_hint": item.get("source_start_hint"),
        })
    clean.sort(key=lambda x: x["start_time"])
    return clean


async def analyze_video(video_path):
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    def work():
        uploaded = _upload_file_sync(path)
        try:
            _wait_until_active_sync(uploaded["name"])
            prompt = """
You are the first-stage source identification engine for an anime clip finder.
Analyze the ENTIRE edited video carefully, frame by frame where useful.

Return ONLY JSON in this shape:
{
  "segments": [
    {
      "start_time": 0.0,
      "end_time": 2.5,
      "anime": "Attack on Titan",
      "season": 4,
      "episode": 5,
      "confidence": 0.95,
      "source_start_hint": null
    }
  ]
}

Rules:
- Preserve exact chronological order.
- Split at every real source-scene change. Do not merge two different source scenes.
- start_time/end_time are timestamps INSIDE THE EDIT, never source-episode timestamps.
- Account for speed-up, slow-down, reverse, zoom, crop, mirror, color grading, overlays,
  subtitles, transitions, repeated frames and short flashes.
- Identify anime, season and episode only when visually supported. Never invent an episode.
- If season/episode is uncertain, use null and lower confidence.
- source_start_hint is optional and must be null unless you have a useful approximate
  position in the original episode from visual recognition alone. It is only a hint,
  not a final timestamp.
- Keep very short shots if they are real source scenes; do not discard them merely because
  they are under one second.
- Confidence must be between 0 and 1.
"""
            return _clean_segments(extract_json(_generate_sync(uploaded, prompt)))
        finally:
            _delete_file_sync(uploaded["name"])

    return await asyncio.to_thread(work)


async def verify_candidate_window(video_path, segment, candidate_start, candidate_end):
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    def work():
        uploaded = _upload_file_sync(path)
        try:
            _wait_until_active_sync(uploaded["name"])
            prompt = f"""
You are the FINAL visual verifier for an anime clip finder.
The uploaded video is a candidate window extracted from a Telegram source episode.
The target came from an edited video.

Target metadata:
- anime: {segment.get('anime') or 'unknown'}
- season: {segment.get('season') or 'unknown'}
- episode: {segment.get('episode') or 'unknown'}
- edited start: {segment.get('start_time')}
- edited end: {segment.get('end_time')}
- candidate source window nominal range: {candidate_start} to {candidate_end} seconds

Analyze visual content, not filenames. The edit may have speed changes, crop/zoom, mirror,
color changes, subtitles, overlays, transitions or removed frames.

Return ONLY JSON:
{{
  "match": true,
  "confidence": 0.96,
  "start_time": 3.25,
  "end_time": 8.10,
  "reason": "brief factual visual evidence"
}}

start_time/end_time MUST be timestamps inside the uploaded candidate window.
Choose the exact visible source-scene boundaries. If the target is not present, return
match=false and confidence below 0.5.
"""
            data = extract_json(_generate_sync(uploaded, prompt))
            return {
                "match": bool(data.get("match")),
                "confidence": max(0.0, min(1.0, float(data.get("confidence", 0)))),
                "start_time": float(data.get("start_time", 0)),
                "end_time": float(data.get("end_time", 0)),
                "reason": str(data.get("reason") or ""),
            }
        finally:
            _delete_file_sync(uploaded["name"])

    return await asyncio.to_thread(work)
