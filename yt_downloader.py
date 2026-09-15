import asyncio
from pathlib import Path

import yt_dlp

from config import TEMP_DIR


async def download_video_from_url(
    url,
    user_id,
):
    output_dir = (
        Path(TEMP_DIR)
        / str(user_id)
        / "input"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_template = str(
        output_dir / "source.%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "format": (
            "bv*[ext=mp4]+ba[ext=m4a]/"
            "b[ext=mp4]/"
            "b"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    def download():
        with yt_dlp.YoutubeDL(
            options
        ) as ydl:
            ydl.download([url])

    await asyncio.to_thread(
        download
    )

    files = list(
        output_dir.glob("*")
    )

    video_files = [
        x
        for x in files
        if x.suffix.lower()
        in (
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
        )
    ]

    if not video_files:
        raise RuntimeError(
            "URL se video download nahi hua."
        )

    return video_files[0]