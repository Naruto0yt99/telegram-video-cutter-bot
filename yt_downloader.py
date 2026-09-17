import asyncio
from pathlib import Path

import yt_dlp

from config import TEMP_DIR


async def download_video_from_url(url, user_id):
    output_dir = Path(TEMP_DIR) / str(user_id) / "input"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / "source.%(ext)s")

    options = {
        "outtmpl": output_template,
        # Gemini does not need 1080p/4K to identify anime scenes. Capping the
        # working copy at 720p saves download time, storage and Gemini upload
        # time while preserving enough visual detail for matching.
        "format": (
            "bv*[ext=mp4][height<=720]+ba[ext=m4a]/"
            "b[ext=mp4][height<=720]/"
            "b[height<=720]/"
            "b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }

    def download():
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

    await asyncio.to_thread(download)

    files = list(output_dir.glob("*"))
    video_files = [
        x for x in files
        if x.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
    ]

    if not video_files:
        raise RuntimeError("URL se video download nahi hua.")

    return video_files[0]
