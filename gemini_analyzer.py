import asyncio
import json
import re
import logging
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from config import GEMINI_API_KEY

logger = logging.getLogger("gemini-analyzer")


def configure_gemini():
    """Initialize Gemini API."""
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)


def upload_video_to_gemini(video_path: str):
    """Upload video to Gemini and return file object."""
    try:
        file = genai.upload_file(
            video_path,
            mime_type="video/mp4",
        )
        logger.info(f"Uploaded video to Gemini: {file.name}")
        return file
    except Exception as e:
        logger.exception(f"Failed to upload video: {e}")
        return None


async def analyze_youtube_short_async(
    video_path: str,
    timeout_seconds: int = 180,
) -> Optional[dict]:
    """
    Analyze a YouTube Short using Gemini to identify ALL source segments.
    
    Returns:
        {
            "segments": [
                {"start_time": 0.5, "end_time": 2.3, "anime": "Naruto", "season": 1, "episode": 5, "confidence": 0.95},
                ...
            ],
            "raw_analysis": "...",
            "error": None or error message
        }
    """
    return await asyncio.to_thread(
        _analyze_youtube_short,
        video_path,
        timeout_seconds,
    )


def _analyze_youtube_short(
    video_path: str,
    timeout_seconds: int = 180,
) -> dict:
    """Synchronous Gemini analysis."""
    configure_gemini()

    video_path = Path(video_path)
    if not video_path.exists():
        return {
            "segments": [],
            "raw_analysis": "",
            "error": f"Video not found: {video_path}",
        }

    # Upload to Gemini
    file = upload_video_to_gemini(str(video_path))
    if not file:
        return {
            "segments": [],
            "raw_analysis": "",
            "error": "Failed to upload video to Gemini",
        }

    try:
        model = genai.GenerativeModel("gemini-2.0-flash-exp")

        prompt = """Analyze this YouTube Short video and identify ALL anime source segments.

For each visible anime scene, provide:
1. Exact start and end timestamps (in seconds, format: 0.0-59.9)
2. Anime title (if recognizable)
3. Season number (if visible or deducible)
4. Episode number (if visible or deducible)
5. Confidence level (0.0-1.0)

Return ONLY valid JSON in this format (no markdown, no extra text):
{
    "segments": [
        {
            "start_time": 0.5,
            "end_time": 2.3,
            "anime": "Naruto",
            "season": 1,
            "episode": 5,
            "confidence": 0.95
        }
    ]
}

If no anime content found, return: {"segments": []}

Important:
- Preserve EXACT order of segments as they appear
- Include ALL visible segments, even if short (>0.5s)
- Mark repeated segments/episodes separately
- Confidence: 1.0 = certain, 0.5 = uncertain, <0.5 = guess only
"""

        response = model.generate_content(
            [prompt, file],
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=2048,
                temperature=0.2,  # Low randomness for consistency
            ),
        )

        raw_text = response.text.strip()

        # Extract JSON from response
        try:
            # Try direct parse
            analysis = json.loads(raw_text)
        except json.JSONDecodeError:
            # Try extracting JSON from markdown code block
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if json_match:
                analysis = json.loads(json_match.group(1))
            else:
                # Try finding any JSON object
                json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                if json_match:
                    analysis = json.loads(json_match.group(0))
                else:
                    raise ValueError("No JSON found in response")

        # Validate segments
        segments = analysis.get("segments", [])
        if not isinstance(segments, list):
            segments = []

        # Filter and sort by start_time
        valid_segments = []
        for seg in segments:
            if (
                isinstance(seg, dict)
                and "start_time" in seg
                and "end_time" in seg
                and isinstance(seg["start_time"], (int, float))
                and isinstance(seg["end_time"], (int, float))
            ):
                valid_segments.append(seg)

        valid_segments.sort(key=lambda x: x["start_time"])

        return {
            "segments": valid_segments,
            "raw_analysis": raw_text,
            "error": None,
        }

    except Exception as e:
        logger.exception("Gemini analysis failed")
        return {
            "segments": [],
            "raw_analysis": "",
            "error": str(e),
        }
    finally:
        # Clean up uploaded file
        try:
            genai.delete_file(file.name)
        except Exception:
            pass


def align_segments_with_library(
    segments: list,
    library_lookup_fn,
) -> list:
    """
    Align Gemini-identified segments with saved episode sources.
    Returns merged list with actual source URLs.
    """
    aligned = []

    for seg in segments:
        anime = seg.get("anime", "").strip()
        season = seg.get("season")
        episode = seg.get("episode")

        if not anime or season is None or episode is None:
            aligned.append({**seg, "source_url": None, "found": False})
            continue

        # Look up in library
        try:
            source_url = library_lookup_fn(anime, str(season), str(episode))
            if source_url:
                aligned.append({**seg, "source_url": source_url, "found": True})
            else:
                aligned.append({**seg, "source_url": None, "found": False})
        except Exception as e:
            logger.warning(f"Library lookup failed for {anime} S{season}E{episode}: {e}")
            aligned.append({**seg, "source_url": None, "found": False})

    return aligned
