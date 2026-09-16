import asyncio
import json
from pathlib import Path

from config import (
    FFMPEG_BIN,
    FFPROBE_BIN,
    TEMP_DIR,
)

from utils import (
    safe_filename,
    unique_path,
)


async def run_command(*args):
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


async def get_duration(video_path):
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
    input_path,
    start,
    end,
    name="clip",
):
    input_path = Path(input_path)

    if start < 0:
        raise ValueError("Start time negative nahi ho sakta.")
    if end <= start:
        raise ValueError("End time start se greater hona chahiye.")

    duration = await get_duration(input_path)
    if end > duration + 0.05:
        raise ValueError(
            f"End time video duration ({format_time(duration)}) se bahar hai."
        )

    output = unique_path(TEMP_DIR, safe_filename(name) + ".mp4")

    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(end - start),
        "-map", "0",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        str(output),
    )

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg clip empty bana raha hai.")

    return output


async def make_clip_exact(
    input_path,
    start,
    end,
    name="clip_exact",
):
    """Frame-accurate clip extraction for FIND verification results."""
    input_path = Path(input_path)
    start = float(start)
    end = float(end)

    if start < 0:
        raise ValueError("Start time negative nahi ho sakta.")
    if end <= start:
        raise ValueError("End time start se greater hona chahiye.")

    duration = await get_duration(input_path)
    if end > duration + 0.05:
        raise ValueError(
            f"End time video duration ({format_time(duration)}) se bahar hai."
        )

    output = unique_path(TEMP_DIR, safe_filename(name) + ".mp4")

    # Input seek plus re-encoding avoids the keyframe-only limitation of -c copy.
    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(end - start),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output),
    )

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg exact clip empty bana raha hai.")

    return output


async def split_video(
    input_path,
    part_duration,
    start=0,
    end=None,
):
    input_path = Path(input_path)

    if part_duration <= 0:
        raise ValueError("Split duration positive hona chahiye.")

    total = await get_duration(input_path)

    if start < 0 or start >= total:
        raise ValueError("Invalid split start.")

    if end is None:
        end = total
    if end <= start:
        raise ValueError("Split end start se greater hona chahiye.")

    end = min(end, total)

    output_dir = Path(TEMP_DIR) / f"split_{input_path.stem}"
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "part_%03d.mp4"

    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss", str(start),
        "-i", str(input_path),
        "-t", str(end - start),
        "-map", "0",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(part_duration),
        "-reset_timestamps", "1",
        str(pattern),
    )

    outputs = sorted(output_dir.glob("part_*.mp4"))
    outputs = [x for x in outputs if x.exists() and x.stat().st_size > 0]
    if not outputs:
        raise RuntimeError("FFmpeg ne split parts nahi banaye.")

    return outputs


async def merge_videos(
    video_paths,
    output_name="merged",
):
    video_paths = [Path(x) for x in video_paths]

    if not video_paths:
        raise ValueError("Merge ke liye koi video nahi hai.")

    list_path = unique_path(TEMP_DIR, "merge_list.txt")
    output = unique_path(TEMP_DIR, safe_filename(output_name) + ".mp4")

    lines = []
    for path in video_paths:
        if not path.exists() or path.stat().st_size == 0:
            raise ValueError(f"Invalid merge input: {path}")
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")

    list_path.write_text("\n".join(lines), encoding="utf-8")

    try:
        await run_command(
            FFMPEG_BIN,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        )
    finally:
        list_path.unlink(missing_ok=True)

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg merge output empty hai.")

    return output


def parse_time(value):
    value = str(value).strip()
    parts = value.split(":")

    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            if seconds >= 60:
                raise ValueError
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            if minutes >= 60 or seconds >= 60:
                raise ValueError
            return hours * 3600 + minutes * 60 + seconds
    except (TypeError, ValueError):
        pass

    raise ValueError(f"Invalid time: {value}")


def format_time(seconds):
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))

    if ms >= 1000:
        whole += 1
        ms = 0

    hours, remainder = divmod(whole, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"
