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
FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.6-flash")
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
            raw_state = data.get("state")
            state = raw_state.get("name") if isinstance(raw_state, dict) else raw_state
            state = str(state or "").upper()
            if state == "ACTIVE":
                return data
            if state == "FAILED":
                raise RuntimeError("Gemini video processing failed.")
            time.sleep(2)
    raise TimeoutError("Gemini video processing timed out.")


def _generate_sync(file_data, prompt, extra_file_data=None):
    _require_key()
    file_datas = [file_data]
    if extra_file_data:
        file_datas.extend(extra_file_data)

    parts = []
    for item in file_datas:
        parts.append({
            "file_data": {
                "mime_type": item.get("mimeType") or item.get("mime_type") or "video/mp4",
                "file_uri": item["uri"],
            }
        })
    parts.append({"text": prompt})
    payload = {"contents": [{"parts": parts}]}

    models = (MODEL,) + tuple(FALLBACK_MODELS)
    retryable_statuses = {429, 500, 502, 503, 504}
    last_error = None

    with httpx.Client(timeout=None) as client:
        for model_index, model in enumerate(models):
            for attempt in range(5):
                try:
                    response = client.post(
                        f"{BASE_URL}/models/{model}:generateContent",
                        headers={
                            "x-goog-api-key": GEMINI_API_KEY,
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if response.status_code in retryable_statuses:
                        body = response.text[:500]
                        last_error = RuntimeError(
                            f"Gemini {model} HTTP {response.status_code}: {body}"
                        )
                        logger.warning(
                            "Gemini generateContent transient error model=%s status=%s attempt=%s/%s",
                            model, response.status_code, attempt + 1, 5,
                        )
                        if attempt < 4:
                            time.sleep(min(2 ** attempt, 16))
                            continue
                        break
                    response.raise_for_status()
                    data = response.json()
                    candidates = data.get("candidates") or []
                    content = candidates[0].get("content") if candidates else None
                    response_parts = content.get("parts") if isinstance(content, dict) else []
                    text = "\n".join(
                        str(part.get("text", "")) for part in (response_parts or []) if part.get("text")
                    )
                    if not text:
                        raise RuntimeError(f"Gemini returned no text: {data}")
                    return text
                except httpx.HTTPError as exc:
                    last_error = exc
                    logger.warning(
                        "Gemini request error model=%s attempt=%s/%s: %s",
                        model, attempt + 1, 5, exc,
                    )
                    if attempt < 4:
                        time.sleep(min(2 ** attempt, 16))
                        continue
                    break
                except Exception as exc:
                    last_error = exc
                    raise
            if model_index < len(models) - 1:
                logger.warning(
                    "Gemini model %s exhausted; trying fallback %s",
                    model, models[model_index + 1],
                )

    raise last_error or RuntimeError("Gemini generateContent failed.")


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


def _clean_regions(value, limit=6):
    if not isinstance(value, list):
        return []
    clean = []
    for region in value[:limit]:
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            continue
        try:
            vals = [float(x) for x in region]
        except Exception:
            continue
        if max(abs(x) for x in vals) > 1.5:
            vals = [x / 100.0 for x in vals]
        x, y, w, h = vals
        if w <= 0 or h <= 0:
            continue
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.05, min(1.0 - x, w))
        h = max(0.05, min(1.0 - y, h))
        clean.append([round(x, 4), round(y, 4), round(w, 4), round(h, 4)])
    return clean


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
        landmarks = item.get("landmarks") or []
        if isinstance(landmarks, str):
            landmarks = [landmarks]
        if not isinstance(landmarks, list):
            landmarks = []
        clean.append({
            "start_time": start,
            "end_time": end,
            "anime": str(item.get("anime") or "").strip(),
            "season": item.get("season"),
            "episode": item.get("episode"),
            "arc": str(item.get("arc") or "").strip(),
            "landmarks": [str(x).strip() for x in landmarks if str(x).strip()][:8],
            "confidence": confidence,
            "source_start_hint": item.get("source_start_hint"),
            "usable_regions": _clean_regions(item.get("usable_regions")),
            "ignored_regions": _clean_regions(item.get("ignored_regions")),
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
Analyze the ENTIRE edited video carefully and identify the ORIGINAL anime episode for every
real source scene. You must also map which parts of the screen contain actual anime footage.

Return ONLY JSON in this shape:
{
  "segments": [
    {
      "start_time": 0.0,
      "end_time": 2.5,
      "anime": "Attack on Titan",
      "season": 4,
      "episode": 5,
      "arc": "",
      "landmarks": ["specific visual/event landmark"],
      "confidence": 0.95,
      "source_start_hint": 123.4,
      "usable_regions": [[0.05,0.10,0.90,0.80]],
      "ignored_regions": [[0.00,0.00,1.00,0.10]]
    }
  ]
}

Region format is [x, y, width, height], normalized from 0 to 1 relative to the video frame.
For every segment, usable_regions MUST contain the smallest practical rectangles containing
actual original anime footage that should be used for visual matching. If the actual footage
fills the frame, use [[0,0,1,1]]. ignored_regions should contain template/background/skull/text/
logo/watermark/decorative areas that are not part of the original anime footage. Do not put
moving anime content into ignored_regions. It is fine for ignored_regions to be empty.

Rules:
- Preserve exact chronological order.
- Split at every real source-scene change. Do not merge different source scenes.
- start_time/end_time are timestamps INSIDE THE EDIT, never source timestamps.
- Account for speed-up, slow-down, reverse, zoom, crop, mirror, color grading, overlays,
  subtitles, transitions, repeated frames and short flashes.
- Identify anime, season and episode only when visually supported. Never invent an episode.
- Distinguish original-series episodes from sequels, movies, specials and fillers when possible.
- Use concrete episode landmarks: location, characters present, costumes/age, exact event,
  fight/action progression, distinctive dialogue context, opening/ending position and scene order.
- Write 1-8 short factual landmarks that can help a second-stage visual search.
- If season/episode is uncertain, use null and lower confidence rather than guessing.
- source_start_hint is an approximate timestamp IN THE ORIGINAL EPISODE. Estimate it only when
  visually supported; allow for different intros/recaps/cuts between source copies. Otherwise null.
- Confidence must be between 0 and 1.
- Template elements must NEVER be treated as source scenes.
- If a skull, text, border, sticker, static background, watermark or logo covers part of the
  frame, identify the remaining actual-anime region instead of splitting the scene unnecessarily.

Important landmark example for Naruto Part 1:
The Chunin Exam Stage 2 / Forest of Death setup immediately before or at the forest gates is
original Naruto episode 27. Episode 28 is the subsequent panic/early-forest action. Use this
kind of exact event-to-episode distinction whenever a landmark is recognizable.
"""
            result = extract_json(_generate_sync(uploaded, prompt))
            return _clean_segments(result)
        finally:
            _delete_file_sync(uploaded["name"])

    return await asyncio.to_thread(work)


async def verify_candidate_window(candidate_video_path, segment, candidate_start, candidate_end, target_video_path=None):
    candidate_path = Path(candidate_video_path)
    if not candidate_path.exists():
        raise FileNotFoundError(str(candidate_path))
    target_path = Path(target_video_path) if target_video_path else None
    if target_path is not None and not target_path.exists():
        raise FileNotFoundError(str(target_path))

    def work():
        candidate_file = _upload_file_sync(candidate_path)
        target_file = None
        try:
            _wait_until_active_sync(candidate_file["name"])
            if target_path is not None:
                target_file = _upload_file_sync(target_path)
                _wait_until_active_sync(target_file["name"])
            prompt = f"""
You are the FINAL visual verifier for an anime clip finder.
Video 1 is the TARGET clip and Video 2 is the CANDIDATE original-anime window.
Compare actual visual content directly, ignoring template/text/background overlays.
Target metadata: anime={segment.get('anime') or 'unknown'}, season={segment.get('season') or 'unknown'},
episode={segment.get('episode') or 'unknown'}, landmarks={segment.get('landmarks') or []}.
Return ONLY JSON: {{"match":true,"confidence":0.96,"start_time":3.25,"end_time":8.10,"reason":"brief factual visual evidence"}}
start_time/end_time are timestamps inside Video 2. The target may be speed-changed, cropped,
zoomed, mirrored, color-graded, subtitled or overlaid. If the candidate does not contain the
same underlying scene, return match=false and confidence <= 0.5.
"""
            if target_file is None:
                raise RuntimeError("Target video required for visual verification.")
            text = _generate_sync(target_file, prompt, extra_file_data=[candidate_file])
            return extract_json(text)
        finally:
            _delete_file_sync(candidate_file["name"])
            if target_file is not None:
                _delete_file_sync(target_file["name"])

    return await asyncio.to_thread(work)
