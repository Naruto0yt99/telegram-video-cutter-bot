import asyncio
import json
import logging
import re
from pathlib import Path

from google import genai

from config import GEMINI_API_KEY

logger = logging.getLogger("gemini-analyzer")

MODEL = "gemini-3.8-flash"


def get_client():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing.")
    return genai.Client(api_key=GEMINI_API_KEY)


def upload_file_sync(client, path):
    return client.files.upload(file=str(path))


def delete_file_sync(client, file_name):
    try:
        client.files.delete(name=file_name)
    except Exception:
        pass


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
        client = get_client()
        uploaded = upload_file_sync(client, path)
        try:
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
            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, uploaded],
            )
            return _clean_segments(extract_json(response.text))
        finally:
            delete_file_sync(client, uploaded.name)

    return await asyncio.to_thread(work)


async def verify_candidate_window(video_path, segment, candidate_start, candidate_end):
    """Ask Gemini whether a downloaded Telegram candidate contains the target scene."""
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    def work():
        client = get_client()
        uploaded = upload_file_sync(client, path)
        try:
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
            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, uploaded],
            )
            data = extract_json(response.text)
            return {
                "match": bool(data.get("match")),
                "confidence": max(0.0, min(1.0, float(data.get("confidence", 0)))),
                "start_time": float(data.get("start_time", 0)),
                "end_time": float(data.get("end_time", 0)),
                "reason": str(data.get("reason") or ""),
            }
        finally:
            delete_file_sync(client, uploaded.name)

    return await asyncio.to_thread(work)
