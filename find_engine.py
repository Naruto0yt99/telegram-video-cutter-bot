import asyncio
import logging
import math
import shutil
from pathlib import Path

from PIL import Image
import numpy as np

from database import (
    get_animes,
    get_seasons,
    get_episodes,
    get_best_source,
)

from telegram_media import (
    parse_telegram_message_link,
    download_telethon_message,
)

from ffmpeg_utils import (
    run_command,
    get_duration,
    make_clip,
    merge_videos,
)

from config import (
    TEMP_DIR,
    FFMPEG_BIN,
)

from gemini_analyzer import (
    analyze_video,
)


logger = logging.getLogger(
    "find-engine"
)


def frame_hash(image):
    image = image.convert(
        "L"
    ).resize(
        (32, 32)
    )

    arr = np.asarray(
        image,
        dtype=np.float32,
    )

    arr -= arr.mean()

    norm = np.linalg.norm(
        arr
    )

    if norm:
        arr /= norm

    return arr


def hash_similarity(a, b):
    denom = (
        np.linalg.norm(a)
        * np.linalg.norm(b)
    )

    if denom == 0:
        return 0.0

    value = float(
        np.dot(a, b) / denom
    )

    return max(
        0.0,
        min(1.0, value),
    )


async def extract_frame(
    video_path,
    timestamp,
    output_path,
):
    await run_command(
        FFMPEG_BIN,
        "-y",
        "-ss",
        str(timestamp),
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-1",
        str(output_path),
    )


async def get_sample_times(
    video_path,
    start,
    end,
    interval=0.75,
):
    duration = end - start

    if duration <= 0:
        return []

    count = max(
        2,
        int(
            math.ceil(
                duration / interval
            )
        ),
    )

    if count == 1:
        return [start]

    return [
        start
        + (
            duration
            * i
            / (count - 1)
        )
        for i in range(count)
    ]


async def compare_segment_to_source(
    edited_path,
    edited_start,
    edited_end,
    source_path,
):
    """
    Finds approximate source interval by comparing
    sampled visual frames.

    Returns:
        {
          "score": ...,
          "source_start": ...,
          "source_end": ...
        }
    """

    temp = Path(TEMP_DIR) / "matcher"

    temp.mkdir(
        parents=True,
        exist_ok=True,
    )

    edited_times = (
        await get_sample_times(
            edited_path,
            edited_start,
            edited_end,
            0.75,
        )
    )

    source_duration = (
        await get_duration(
            source_path
        )
    )

    # Coarse source scan.
    source_interval = 2.0

    source_times = list(
        np.arange(
            0,
            max(
                0,
                source_duration - 0.1
            ),
            source_interval,
        )
    )

    if not edited_times:
        raise RuntimeError(
            "Edited segment empty."
        )

    edited_hashes = []

    for i, timestamp in enumerate(
        edited_times
    ):
        frame_path = (
            temp
            / f"edited_{i}.jpg"
        )

        await extract_frame(
            edited_path,
            timestamp,
            frame_path,
        )

        try:
            image = Image.open(
                frame_path
            ).convert("RGB")

            edited_hashes.append(
                frame_hash(image)
            )
        finally:
            frame_path.unlink(
                missing_ok=True
            )

    best_score = -1.0
    best_time = None

    # Compare a subset of edited frames
    # against coarse source frames.
    source_step = max(
        1,
        len(source_times) // 600,
    )

    for source_time in source_times[
        ::source_step
    ]:
        frame_path = (
            temp
            / "source.jpg"
        )

        await extract_frame(
            source_path,
            float(source_time),
            frame_path,
        )

        try:
            image = Image.open(
                frame_path
            ).convert("RGB")

            source_hash = frame_hash(
                image
            )

            score = float(
                np.mean(
                    [
                        hash_similarity(
                            h,
                            source_hash,
                        )
                        for h in edited_hashes
                    ]
                )
            )

            if score > best_score:
                best_score = score
                best_time = float(
                    source_time
                )

        finally:
            frame_path.unlink(
                missing_ok=True
            )

    if best_time is None:
        raise RuntimeError(
            "Source visual match nahi mila."
        )

    # Refine around coarse hit.
    refine_start = max(
        0,
        best_time - 3,
    )

    refine_end = min(
        source_duration,
        best_time + 3,
    )

    refine_times = list(
        np.arange(
            refine_start,
            refine_end,
            0.25,
        )
    )

    best_refined = best_time
    best_refined_score = best_score

    for source_time in refine_times:
        frame_path = (
            temp
            / "refine.jpg"
        )

        await extract_frame(
            source_path,
            float(source_time),
            frame_path,
        )

        try:
            image = Image.open(
                frame_path
            ).convert("RGB")

            source_hash = frame_hash(
                image
            )

            score = float(
                np.mean(
                    [
                        hash_similarity(
                            h,
                            source_hash,
                        )
                        for h in edited_hashes
                    ]
                )
            )

            if score > best_refined_score:
                best_refined_score = score
                best_refined = float(
                    source_time
                )

        finally:
            frame_path.unlink(
                missing_ok=True
            )

    # Approximate source duration based on
    # edited segment duration.
    segment_duration = (
        edited_end
        - edited_start
    )

    source_start = max(
        0,
        best_refined - 0.5,
    )

    source_end = min(
        source_duration,
        source_start
        + segment_duration
        + 1.0,
    )

    return {
        "score": best_refined_score,
        "source_start": source_start,
        "source_end": source_end,
    }


def candidate_episodes(
    anime,
    season,
    episode,
):
    if not anime:
        return []

    if season is not None:
        seasons = [
            str(season)
        ]
    else:
        seasons = get_seasons(
            anime
        )

    candidates = []

    for s in seasons:
        if episode is not None:
            episodes = [
                str(episode)
            ]
        else:
            episodes = get_episodes(
                anime,
                s,
            )

        for e in episodes:
            source = get_best_source(
                anime,
                s,
                e,
            )

            if source:
                candidates.append(
                    {
                        "anime": anime,
                        "season": s,
                        "episode": e,
                        "source_url": source,
                    }
                )

    return candidates


async def find_and_build(
    input_video,
    user_id,
    telethon_client,
    progress_message=None,
):
    if telethon_client is None:
        raise RuntimeError(
            "Telegram source client connected nahi hai."
        )

    input_video = Path(
        input_video
    )

    if not input_video.exists():
        raise RuntimeError(
            "Input video nahi mila."
        )

    segments = await analyze_video(
        input_video
    )

    if not segments:
        raise RuntimeError(
            "Gemini ko koi usable anime segment nahi mila."
        )

    job_dir = (
        Path(TEMP_DIR)
        / str(user_id)
        / "find_job"
    )

    if job_dir.exists():
        shutil.rmtree(
            job_dir,
            ignore_errors=True,
        )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    clips = []
    matched = 0

    try:
        for index, segment in enumerate(
            segments,
            start=1,
        ):
            anime = segment.get(
                "anime"
            )

            season = segment.get(
                "season"
            )

            episode = segment.get(
                "episode"
            )

            if not anime:
                continue

            candidates = candidate_episodes(
                anime,
                season,
                episode,
            )

            if not candidates:
                continue

            if progress_message:
                try:
                    await progress_message.edit_text(
                        f"🎯 FIND\n\n"
                        f"Scene {index}/{len(segments)}\n"
                        f"{anime} "
                        f"S{season or '?'} "
                        f"E{episode or '?'}\n\n"
                        "Telegram source download/matching..."
                    )
                except Exception:
                    pass

            best = None

            for candidate in candidates:
                try:
                    chat, message_id = (
                        parse_telegram_message_link(
                            candidate[
                                "source_url"
                            ]
                        )
                    )

                    source_path = (
                        await download_telethon_message(
                            telethon_client,
                            chat,
                            message_id,
                            user_id,
                        )
                    )

                    result = (
                        await compare_segment_to_source(
                            input_video,
                            float(
                                segment[
                                    "start_time"
                                ]
                            ),
                            float(
                                segment[
                                    "end_time"
                                ]
                            ),
                            source_path,
                        )
                    )

                    score = result[
                        "score"
                    ]

                    if (
                        best is None
                        or score
                        > best["score"]
                    ):
                        best = {
                            **candidate,
                            **result,
                            "source_path":
                                source_path,
                        }

                except Exception as exc:
                    logger.warning(
                        "Candidate failed: %s",
                        exc,
                    )

            if not best:
                continue

            # Conservative threshold.
            if best["score"] < 0.82:
                logger.warning(
                    "Weak visual match: %.3f",
                    best["score"],
                )
                continue

            clip_path = (
                await make_clip(
                    best["source_path"],
                    best[
                        "source_start"
                    ],
                    best[
                        "source_end"
                    ],
                    name=f"find_{index}",
                )
            )

            clips.append(
                clip_path
            )

            matched += 1

            try:
                best[
                    "source_path"
                ].unlink(
                    missing_ok=True
                )
            except Exception:
                pass

        if not clips:
            raise RuntimeError(
                "Koi reliable source clip match nahi mila."
            )

        output = await merge_videos(
            clips,
            "find_result",
        )

        return {
            "output": output,
            "matched": matched,
            "total": len(segments),
        }

    finally:
        for clip in clips:
            try:
                clip.unlink(
                    missing_ok=True
                )
            except Exception:
                pass