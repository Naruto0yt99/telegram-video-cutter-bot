import asyncio
import json
from pathlib import Path

from config import FFMPEG_BIN, FFPROBE_BIN, TEMP_DIR
from utils import safe_filename, unique_path


async def run_command(*args: str):
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error = stderr.decode(errors="replace").strip()
        raise RuntimeError(error or f"Command failed: {args[0]}")

    return stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def get_duration(video_path: str | Path) -> float:
    stdout, _ = await run_command(
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    )

    data = json.loads(stdout)
    return float(data["format"]["duration"])


async def make_clip(
    input_path: str | Path,
    start: float,
    end: float,
    name: str = "clip",
) -> Path:
    input_path = Path(input_path)

    if start < 0:
        raise ValueError("Start time cannot be negative.")

    if end <= start:
        raise ValueError("End time must be greater than start time.")

    duration = await get_duration(input_path)

    if end > duration + 0.05:
        raise ValueError(
            f"End time exceeds video duration ({format_time(duration)})."
        )

    clip_duration = end - start

    output = unique_path(
        TEMP_DIR,
        safe_filename(name) + ".mp4",
    )

    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(clip_duration),
        "-map", "0",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output),
    )

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg produced an empty clip.")

    return output


async def split_video(
    input_path: str | Path,
    part_duration: int,
    start: float = 0,
    end: float | None = None,
) -> list[Path]:

    input_path = Path(input_path)

    if part_duration <= 0:
        raise ValueError("Split duration must be positive.")

    total_duration = await get_duration(input_path)

    if start < 0 or start >= total_duration:
        raise ValueError("Invalid split start time.")

    if end is None:
        end = total_duration

    if end <= start:
        raise ValueError("Split end must be greater than start.")

    end = min(end, total_duration)

    output_dir = Path(TEMP_DIR) / f"split_{input_path.stem}"
    output_dir.mkdir(parents=True, exist_ok=True)

    pattern = output_dir / "part_%03d.mp4"

    duration = end - start

    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(duration),
        "-map", "0",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(part_duration),
        "-reset_timestamps", "1",
        str(pattern),
    )

    outputs = sorted(output_dir.glob("part_*.mp4"))

    outputs = [
        path for path in outputs
        if path.exists() and path.stat().st_size > 0
    ]

    if not outputs:
        raise RuntimeError("FFmpeg did not create split parts.")

    return outputs


def parse_time(value: str) -> float:
    value = value.strip()

    if not value:
        raise ValueError("Empty time.")

    parts = value.split(":")

    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise ValueError(f"Invalid time: {value}")

    if len(numbers) == 1:
        seconds = numbers[0]

    elif len(numbers) == 2:
        minutes, seconds_part = numbers
        if minutes < 0 or seconds_part < 0 or seconds_part >= 60:
            raise ValueError(f"Invalid time: {value}")
        seconds = minutes * 60 + seconds_part

    elif len(numbers) == 3:
        hours, minutes, seconds_part = numbers

        if (
            hours < 0
            or minutes < 0
            or seconds_part < 0
            or minutes >= 60
            or seconds_part >= 60
        ):
            raise ValueError(f"Invalid time: {value}")

        seconds = hours * 3600 + minutes * 60 + seconds_part

    else:
        raise ValueError(f"Invalid time: {value}")

    if seconds < 0:
        raise ValueError(f"Invalid time: {value}")

    return seconds


def format_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    return f"{minutes:02d}:{secs:02d}"
