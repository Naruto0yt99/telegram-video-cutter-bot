import asyncio
from pathlib import Path

import yt_dlp

from config import TEMP_DIR


async def download_video_from_url(url, user_id):
    output_dir = Path(TEMP_DIR) / str(user_id) / "input"
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / "source.%(ext)s")

    # Prefer one 360p stream to avoid a separate video+audio download.
    # Fall back to the previous split-stream selector if needed.
    format_candidates = (
        "b[ext=mp4][height<=360]/b[height<=360]/b[ext=mp4][height<=720]/b[height<=720]/b",
        "bv*[ext=mp4][height<=360]+ba[ext=m4a]/b[ext=mp4][height<=360]/b[height<=360]/b",
    )

    last_error = None

    for format_selector in format_candidates:
        for old in output_dir.glob("*"):
            try:
                old.unlink()
            except OSError:
                pass

        options = {
            "outtmpl": output_template,
            "format": format_selector,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 4,
            "fragment_retries": 4,
            "socket_timeout": 45,
            "continuedl": False,
        }

        def download():
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])

        try:
            await asyncio.to_thread(download)
        except Exception as exc:
            last_error = exc
            continue

        files = [
            x for x in output_dir.glob("*")
            if x.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
            and x.stat().st_size > 0
        ]
        if files:
            return files[0]

    if last_error:
        raise RuntimeError(f"YouTube download failed: {last_error}") from last_error
    raise RuntimeError("URL se video download nahi hua.")
