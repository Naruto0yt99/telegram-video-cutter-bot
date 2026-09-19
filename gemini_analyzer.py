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

MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ("gemini-3.5-flash",)
API_ROOT = "https://generativelanguage.googleapis.com"
MODEL_ATTEMPTS = 1
REQUEST_TIMEOUT = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)


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
    deadline = time.monotonic() + 120.0
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
        # LOW was too aggressive for anime scene recognition. Keep candidate
        # verification lean, but give the initial edit-analysis pass enough
        # visual detail to distinguish characters, locations and actions.
        "generationConfig": {
            "responseMimeType": "application/json",
            "mediaResolution": "MEDIA_RESOLUTION_MEDIUM",
        },
    }
    return _generate_with_fallback(payload)


def verify_source_match(edit_path: Path, source_path: Path, source_window_start: float):
    if not edit_path.exists() or not source_path.exists():
        return None
    edit_file = _upload_file(edit_path)
    source_file = _upload_file(source_path)
    edit_name = edit_file.get("name")
    source_name = source_file.get("name")
    if not edit_name or not source_name:
        return None
    _wait_file_active(edit_name)
    _wait_file_active(source_name)
    prompt = f"""
Compare these two videos. VIDEO 1 is an edited anime clip; VIDEO 2 is a candidate
window from the original episode. The edit may contain crop, zoom, subtitles,
speed changes, color changes, overlays, or transitions.

The candidate window starts at {float(source_window_start):.3f} seconds in the episode.
Find the exact original interval represented by VIDEO 1 inside VIDEO 2. Return source_duration as the original interval length and speed as original_duration / edited_duration.

Return ONLY JSON:
{{"match":true,"confidence":0.0,"offset_in_source_window":0.0,"source_duration":0.0,"speed":1.0}}

Use visual action, characters, camera movement, and scene continuity. Ignore music,
subtitles, logos, and editing effects. Do not mark a match merely because the
characters/anime are similar.
"""
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{edit_name}"}},
                {"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{source_name}"}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    data = _generate_with_fallback(payload)
    parsed = _parse_json(_text_from_response(data))
    if not isinstance(parsed, dict):
        return None
    try:
        parsed["confidence"] = float(parsed.get("confidence", 0.0) or 0.0)
        parsed["offset_in_source_window"] = float(parsed.get("offset_in_source_window", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    parsed["match"] = bool(parsed.get("match"))
    return parsed


async def verify_source_candidates(edit_path: Path, candidate_paths, candidate_starts):
    if not edit_path.exists() or not candidate_paths:
        return None
    if len(candidate_paths) != len(candidate_starts):
        raise ValueError("candidate_paths and candidate_starts length mismatch")

    edit_file = _upload_file(edit_path)
    edit_name = edit_file.get("name")
    if not edit_name:
        return None

    existing = [
        (idx, Path(path), float(candidate_starts[idx]))
        for idx, path in enumerate(candidate_paths)
        if Path(path).exists()
    ]
    uploaded_results = await asyncio.gather(
        *(asyncio.to_thread(_upload_file, path) for _, path, _ in existing),
        return_exceptions=True,
    )
    candidate_files = []
    candidate_meta = []
    for (original_index, path, start_time), uploaded in zip(existing, uploaded_results):
        if isinstance(uploaded, Exception):
            logger.warning("Gemini candidate upload failed index=%s: %s", original_index, uploaded)
            continue
        name = uploaded.get("name")
        if name:
            candidate_files.append(name)
            candidate_meta.append((len(candidate_files), original_index, start_time))

    if not candidate_files:
        return None

    await asyncio.gather(
        asyncio.to_thread(_wait_file_active, edit_name),
        *(asyncio.to_thread(_wait_file_active, name) for name in candidate_files),
    )

    candidate_lines = [
        f"Candidate {display_index}: starts at {float(start_time):.3f}s in the episode."
        for display_index, _, start_time in candidate_meta
    ]

    prompt = f"""
VIDEO 1 is the edited anime scene.
VIDEOS 2 onward are candidate windows from the same original episode.
{chr(10).join(candidate_lines)}

Find which candidate contains the exact visual scene from VIDEO 1. The edit may
have crop, zoom, subtitles, color changes, overlays, transitions, or speed
changes. Ignore music and text overlays.

Return ONLY JSON:
{{"match":true,"candidate_index":1,"confidence":0.0,"offset_in_candidate":0.0,"source_duration":0.0,"speed":1.0}}

candidate_index is the numbered candidate that contains the match.
offset_in_candidate is seconds from that candidate's start.
source_duration is the original duration represented by VIDEO 1.
speed is original_duration / edited_duration.

Do not mark a match merely because the characters or anime are similar. Use
specific visual action, camera movement, character poses, and scene continuity.
"""

    parts = [{"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{edit_name}"}}]
    parts.extend(
        {"file_data": {"mime_type": "video/mp4", "file_uri": f"{API_ROOT}/v1beta/{name}"}}
        for name in candidate_files
    )
    parts.append({"text": prompt})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    data = _generate_with_fallback(payload)
    parsed = _parse_json(_text_from_response(data))
    if not isinstance(parsed, dict):
        return None

    try:
        parsed["candidate_index"] = int(parsed.get("candidate_index", 0) or 0)
        parsed["confidence"] = float(parsed.get("confidence", 0.0) or 0.0)
        parsed["offset_in_candidate"] = float(parsed.get("offset_in_candidate", 0.0) or 0.0)
        parsed["source_duration"] = float(parsed.get("source_duration", 0.0) or 0.0)
        parsed["speed"] = float(parsed.get("speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        return None
    parsed["match"] = bool(parsed.get("match"))
    return parsed


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
Watch the uploaded YouTube anime edit carefully and identify every contiguous
source shot. The goal is to make a searchable scene fingerprint, not merely to
guess a timestamp.

For EACH shot return:
- edit start/end
- anime
- season/episode when identifiable
- best approximate original timestamp
- speed
- a concise but highly distinctive scene description
- characters visible
- location/background
- important action/event sequence
- distinctive objects or visual landmarks
- what happens immediately before and after the main action

Describe actions in chronological order. Use concrete visual facts rather than
generic labels such as "Naruto scene" or "fight scene". If the exact episode is
uncertain, still provide your best anime/arc/episode estimate and detailed visual
description. Never return an empty list merely because the timestamp is uncertain.

Return ONLY JSON:
{{
  "regions": [
    {{
      "start_time": 0,
      "end_time": 5,
      "anime": "NARUTO",
      "season": 1,
      "episode": 27,
      "source_start_hint": 755,
      "confidence": 0.9,
      "speed": 1.0,
      "description": "Distinctive chronological description of the shot.",
      "characters": ["Naruto"],
      "location": "forest",
      "actions": ["..."],
      "landmarks": ["..."]
    }}
  ]
}}

Catalog: {catalog}
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
    logger.info("Gemini detailed scene-analysis pass=1")
    data = _generate_video_prompt(name, _analysis_prompt(_catalog_text()), temperature=0.0)
    parsed = _parse_json(_text_from_response(data))
    regions = parsed.get("regions") if isinstance(parsed, dict) else None

    if not isinstance(regions, list) or not regions:
        logger.warning("Gemini pass=1 returned no regions; running permissive fallback")
        catalog = _catalog_text()
        fallback_prompt = f"""
Watch the uploaded video and identify the anime footage in it.
This is a BEST-EFFORT extraction task. Return every visible source shot even if
the exact episode or timestamp is uncertain.

For every shot include start_time, end_time, anime, season, episode,
source_start_hint, confidence, speed, and a concise description of the visible
action, characters, location and distinctive landmarks.

Return JSON only:
{{
  "regions": [
    {{
      "start_time": 0,
      "end_time": 5,
      "anime": "NARUTO",
      "season": 1,
      "episode": 27,
      "source_start_hint": 755,
      "confidence": 0.5,
      "speed": 1.0,
      "description": "Visible action and scene landmarks."
    }}
  ]
}}

Do not refuse because anything is uncertain and do not return an empty regions
array when anime footage is visible.

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
