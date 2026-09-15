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
        error = stderr.decode(
            errors="replace"
        ).strip()

        raise RuntimeError(
            error or f"Command failed: {args[0]}"
        )

    return (
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def get_duration(video_path):
    stdout, _ = await run_command(
        FFPROBE_BIN,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    )

    data = json.loads(stdout)

    return float(
        data["format"]["duration"]
    )


async def make_clip(
    input_path,
    start,
    end,
    name="clip",
):
    input_path = Path(input_path)

    if start < 0:
        raise ValueError(
            "Start time negative nahi ho sakta."
        )

    if end <= start:
        raise ValueError(
            "End time start se greater hona chahiye."
        )

    duration = await get_duration(
        input_path
    )

    if end > duration + 0.05:
        raise ValueError(
            f"End time video duration "
            f"({format_time(duration)}) se bahar hai."
        )

    output = unique_path(
        TEMP_DIR,
        safe_filename(name) + ".mp4",
    )

    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss",
        str(start),
        "-i",
        str(input_path),
        "-t",
        str(end - start),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    )

    if (
        not output.exists()
        or output.stat().st_size == 0
    ):
        raise RuntimeError(
            "FFmpeg clip empty bana raha hai."
        )

    return output


async def split_video(
    input_path,
    part_duration,
    start=0,
    end=None,
):
    input_path = Path(input_path)

    if part_duration <= 0:
        raise ValueError(
            "Split duration positive hona chahiye."
        )

    total = await get_duration(
        input_path
    )

    if start < 0 or start >= total:
        raise ValueError(
            "Invalid split start."
        )

    if end is None:
        end = total

    if end <= start:
        raise ValueError(
            "Split end start se greater hona chahiye."
        )

    end = min(
        end,
        total,
    )

    output_dir = (
        Path(TEMP_DIR)
        / f"split_{input_path.stem}"
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    pattern = (
        output_dir
        / "part_%03d.mp4"
    )

    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss",
        str(start),
        "-i",
        str(input_path),
        "-t",
        str(end - start),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(part_duration),
        "-reset_timestamps",
        "1",
        str(pattern),
    )

    outputs = sorted(
        output_dir.glob("part_*.mp4")
    )

    outputs = [
        x
        for x in outputs
        if x.exists()
        and x.stat().st_size > 0
    ]

    if not outputs:
        raise RuntimeError(
            "FFmpeg ne split parts nahi banaye."
        )

    return outputs


async def merge_videos(
    video_paths,
    output_name="merged",
):
    video_paths = [
        Path(x)
        for x in video_paths
    ]

    if not video_paths:
        raise ValueError(
            "Merge ke liye videos nahi hain."
        )

    output = unique_path(
        TEMP_DIR,
        safe_filename(output_name)
        + ".mp4",
    )

    concat_file = unique_path(
        TEMP_DIR,
        "concat.txt",
    )

    try:
        with concat_file.open(
            "w",
            encoding="utf-8",
        ) as f:
            for path in video_paths:
                escaped = (
                    str(path)
                    .replace("'", "'\\''")
                )

                f.write(
                    f"file '{escaped}'\n"
                )

        await run_command(
            FFMPEG_BIN,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output),
        )

        if (
            not output.exists()
            or output.stat().st_size == 0
        ):
            raise RuntimeError(
                "Merge output empty hai."
            )

        return output

    finally:
        concat_file.unlink(
            missing_ok=True
        )


def parse_time(value):
    value = value.strip()

    parts = value.split(":")

    try:
        nums = [
            float(x)
            for x in parts
        ]
    except ValueError:
        raise ValueError(
            f"Invalid time: {value}"
        )

    if len(nums) == 1:
        seconds = nums[0]

    elif len(nums) == 2:
        minutes, sec = nums

        if (
            minutes < 0
            or sec < 0
            or sec >= 60
        ):
            raise ValueError(
                f"Invalid time: {value}"
            )

        seconds = (
            minutes * 60
            + sec
        )

    elif len(nums) == 3:
        hours, minutes, sec = nums

        if (
            hours < 0
            or minutes < 0
            or sec < 0
            or minutes >= 60
            or sec >= 60
        ):
            raise ValueError(
                f"Invalid time: {value}"
            )

        seconds = (
            hours * 3600
            + minutes * 60
            + sec
        )

    else:
        raise ValueError(
            f"Invalid time: {value}"
        )

    if seconds < 0:
        raise ValueError(
            f"Invalid time: {value}"
        )

    return seconds


def format_time(seconds):
    seconds = max(
        0,
        int(round(seconds)),
    )

    hours = seconds // 3600
    minutes = (
        seconds % 3600
    ) // 60
    secs = seconds % 60

    if hours:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{secs:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{secs:02d}"
    )