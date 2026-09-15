import asyncio
import json
import logging
import re
from pathlib import Path

from google import genai

from config import GEMINI_API_KEY


logger = logging.getLogger(
    "gemini-analyzer"
)


def get_client():
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY missing."
        )

    return genai.Client(
        api_key=GEMINI_API_KEY
    )


def upload_file_sync(
    client,
    path,
):
    return client.files.upload(
        file=str(path)
    )


def delete_file_sync(
    client,
    file_name,
):
    try:
        client.files.delete(
            name=file_name
        )
    except Exception:
        pass


def extract_json(text):
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(
        r"\{.*\}",
        text,
        re.DOTALL,
    )

    if not match:
        raise ValueError(
            "Gemini response me JSON nahi mila."
        )

    return json.loads(
        match.group(0)
    )


async def analyze_video(
    video_path,
):
    path = Path(video_path)

    if not path.exists():
        raise FileNotFoundError(
            str(path)
        )

    def work():
        client = get_client()

        uploaded = upload_file_sync(
            client,
            path,
        )

        try:
            prompt = """
Analyze this edited anime video.

We need to identify the individual source scenes
that appear in the edit.

Return ONLY JSON:

{
  "segments": [
    {
      "start_time": 0.0,
      "end_time": 2.5,
      "anime": "Naruto",
      "season": 1,
      "episode": 5,
      "confidence": 0.95
    }
  ]
}

Rules:

- Preserve exact chronological order.
- Split whenever the source scene changes.
- start_time/end_time refer to THIS EDITED VIDEO.
- Anime/season/episode should only be supplied when reasonably identifiable.
- If season or episode is unknown, use null.
- Never invent an episode number merely to fill the field.
- Confidence must be 0.0 to 1.0.
"""

            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=[
                    prompt,
                    uploaded,
                ],
            )

            data = extract_json(
                response.text
            )

            segments = data.get(
                "segments",
                [],
            )

            clean = []

            for item in segments:
                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                try:
                    start = float(
                        item["start_time"]
                    )

                    end = float(
                        item["end_time"]
                    )

                except Exception:
                    continue

                if end <= start:
                    continue

                clean.append(
                    {
                        "start_time": start,
                        "end_time": end,
                        "anime": (
                            item.get(
                                "anime"
                            )
                            or ""
                        ).strip(),
                        "season": item.get(
                            "season"
                        ),
                        "episode": item.get(
                            "episode"
                        ),
                        "confidence": float(
                            item.get(
                                "confidence",
                                0,
                            )
                        ),
                    }
                )

            clean.sort(
                key=lambda x:
                x["start_time"]
            )

            return clean

        finally:
            delete_file_sync(
                client,
                uploaded.name,
            )

    return await asyncio.to_thread(
        work
    )