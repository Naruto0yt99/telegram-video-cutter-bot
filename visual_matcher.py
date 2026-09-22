import asyncio
from pathlib import Path

import numpy as np
from PIL import Image

from config import FFMPEG_BIN
from ffmpeg_utils import run_command

REMOTE_HTTP_OPTIONS = [
    "-seekable", "1",
    "-multiple_requests", "1",
    "-initial_request_size", str(2 * 1024 * 1024),
    "-request_size", str(2 * 1024 * 1024),
    "-short_seek_size", str(2 * 1024 * 1024),
]


def _normalize_region(region):
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return None
    try:
        x, y, w, h = [float(v) for v in region]
    except Exception:
        return None
    if max(abs(x), abs(y), abs(w), abs(h)) > 1.5:
        x, y, w, h = [v / 100.0 for v in (x, y, w, h)]
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    w = max(0.05, min(1.0 - x, w))
    h = max(0.05, min(1.0 - y, h))
    return x, y, w, h


def _crop_region(image, region):
    region = _normalize_region(region)
    if not region:
        return image
    x, y, w, h = region
    left = int(image.width * x)
    top = int(image.height * y)
    right = max(left + 2, int(image.width * (x + w)))
    bottom = max(top + 2, int(image.height * (y + h)))
    return image.crop((left, top, min(image.width, right), min(image.height, bottom)))


def _signature(image):
    image = image.convert("RGB")
    small = image.resize((32, 18), Image.Resampling.BILINEAR)
    arr = np.asarray(small, dtype=np.float32) / 255.0
    gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    gray = (gray - gray.mean()) / (gray.std() + 1e-6)
    gx = np.diff(gray, axis=1, prepend=gray[:, :1])
    gy = np.diff(gray, axis=0, prepend=gray[:1, :])
    edge = np.sqrt(gx * gx + gy * gy)
    edge = edge / (edge.mean() + 1e-6)

    hist_parts = []
    for channel in range(3):
        hist, _ = np.histogram(arr[:, :, channel], bins=8, range=(0.0, 1.0))
        hist = hist.astype(np.float32)
        hist /= hist.sum() + 1e-6
        hist_parts.append(hist)

    return np.concatenate([
        gray.reshape(-1),
        edge.reshape(-1),
        np.concatenate(hist_parts),
    ]).astype(np.float32)


def _distance(a, b):
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8
    cosine = float(np.dot(a, b) / denom)
    return 1.0 - max(-1.0, min(1.0, cosine))


def _frame_signatures(path, usable_regions=None):
    image = Image.open(path).convert("RGB")
    regions = [_normalize_region(r) for r in (usable_regions or [])]
    regions = [r for r in regions if r]
    if not regions:
        regions = [(0.0, 0.0, 1.0, 1.0)]

    signatures = [_signature(_crop_region(image, region)) for region in regions]
    full = _signature(image)
    signatures.append(full)
    return signatures


def _best_frame_match(target_groups, source_groups):
    best = 99.0
    for target_sigs in target_groups:
        for source_sigs in source_groups:
            for a in target_sigs:
                for b in source_sigs:
                    best = min(best, _distance(a, b))
    return best


def _sequence_score(target_groups, source_groups, source_times):
    if not target_groups or not source_groups:
        return None

    matches = []
    for target in target_groups:
        best_idx = -1
        best_dist = 99.0
        for idx, source in enumerate(source_groups):
            d = _best_frame_match(target, source)
            if d < best_dist:
                best_dist = d
                best_idx = idx
        if best_idx >= 0:
            matches.append((best_idx, best_dist))

    if not matches:
        return None

    indices = [m[0] for m in matches]
    monotonic = sum(1 for a, b in zip(indices, indices[1:]) if b >= a)
    progression = monotonic / max(1, len(indices) - 1)
    distances = [m[1] for m in matches]
    median_dist = float(np.median(distances))
    p25 = float(np.percentile(distances, 25))
    coverage = sum(1 for d in distances if d <= 0.40) / max(1, len(distances))
    score = median_dist * 0.55 + p25 * 0.10 + (1.0 - progression) * 0.15 + (1.0 - coverage) * 0.20
    return {
        "score": score,
        "median_distance": median_dist,
        "progression": progression,
        "coverage": coverage,
        "indices": indices,
        "source_time": source_times[indices[len(indices) // 2]],
    }


async def _extract_frames(input_path, out_dir, fps=1.0, start=None, duration=None, remote=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "frame_%06d.jpg"
    args = [FFMPEG_BIN, "-y"]
    if remote:
        args += REMOTE_HTTP_OPTIONS
    if start is not None:
        args += ["-ss", str(max(0.0, start))]
    args += ["-i", str(input_path)]
    if duration is not None:
        args += ["-t", str(max(0.1, duration))]
    args += [
        "-vf", f"fps={max(0.25, float(fps))},scale=256:144:force_original_aspect_ratio=decrease,pad=256:144:(ow-iw)/2:(oh-ih)/2",
        "-q:v", "5",
        str(pattern),
    ]
    await run_command(*args)
    return sorted(out_dir.glob("frame_*.jpg"))


async def _load_signatures(frame_paths, usable_regions=None):
    groups = []
    for path in frame_paths:
        try:
            groups.append(_frame_signatures(path, usable_regions))
        except Exception:
            continue
    return groups


async def _search_window(server_url, target_groups, usable_regions, start, duration, root, fps=0.5):
    source_dir = root / f"source_{int(start * 10)}_{int(fps * 10)}"
    source_frames = await _extract_frames(
        server_url, source_dir, fps=fps, start=start, duration=duration, remote=True
    )
    source_groups = await _load_signatures(source_frames, usable_regions)
    if not source_groups:
        return None
    source_times = [start + i / fps for i in range(len(source_groups))]
    return _sequence_score(target_groups, source_groups, source_times)


def _unique_starts(values, max_start):
    result = []
    seen = set()
    for value in values:
        value = round(max(0.0, min(float(value), max_start)), 1)
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _distinct_results(results, distance=10.0):
    chosen = []
    for result in sorted(results, key=lambda x: x["score"]):
        if all(abs(float(result["source_time"]) - float(item["source_time"])) >= distance for item in chosen):
            chosen.append(result)
        if len(chosen) >= 4:
            break
    return chosen


async def _parallel_search_windows(
    server_url,
    starts,
    target_groups,
    usable_regions,
    window,
    source_duration,
    job_dir,
    fps,
    concurrency=2,
):
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def search_one(start):
        async with semaphore:
            try:
                result = await _search_window(
                    server_url,
                    target_groups,
                    usable_regions,
                    start,
                    min(window, max(1.0, source_duration - start)),
                    job_dir,
                    fps=fps,
                )
                return result
            except Exception as exc:
                logger = __import__("logging").getLogger("visual-matcher")
                logger.info("visual window failed start=%s: %s", start, exc)
                return None

    results = await asyncio.gather(*(search_one(start) for start in starts))
    return [result for result in results if result]


async def find_visual_match(client, candidate, segment, target_video, job_dir, source_duration):
    """Hierarchical remote visual retrieval without full-episode fingerprinting.

    The search is intentionally bounded:
    - one Telegram range server is reused for coarse + refine passes;
    - coarse windows run with small bounded concurrency;
    - only the best distinct coarse hits are refined at higher FPS;
    - no persistent full-episode fingerprint is required for /find.
    """
    from telegram_remote import open_telegram_range_server

    target_duration = max(
        1.0,
        float(segment["end_time"]) - float(segment["start_time"]),
    )
    target_dir = job_dir / f"target_frames_{int(float(segment['start_time']) * 10)}"
    target_fps = 1.5 if target_duration <= 20 else 0.75
    target_frames = await _extract_frames(target_video, target_dir, fps=target_fps)
    if not target_frames:
        return None

    usable_regions = segment.get("usable_regions") or []
    target_groups = await _load_signatures(target_frames, usable_regions)
    if not target_groups:
        return None

    server = None
    try:
        server = await open_telegram_range_server(client, candidate["source_url"])
        if not source_duration or source_duration <= 0:
            source_duration = float(server.duration)

        hint = segment.get("source_start_hint")
        try:
            hint = None if hint is None else float(hint)
        except (TypeError, ValueError):
            hint = None
        if hint is not None:
            hint = max(0.0, min(hint, max(0.0, source_duration - 1.0)))

        window = max(28.0, min(75.0, target_duration * 2.8 + 14.0))
        max_start = max(0.0, source_duration - window)

        if hint is not None:
        # Keep the hint path tight. The old implementation tried 11 windows;
        # this covers the local neighborhood plus progressively wider recovery.
        starts = _unique_starts(
            [
                hint - window / 2,
                hint,
                hint + window / 2,
                hint - 45,
                hint + 45,
                hint - 120,
                hint + 120,
            ],
            max_start,
        )
    else:
        # No hint: sparse whole-episode coverage. Windows are intentionally
        # larger than the stride so neighboring windows overlap enough to catch
        # scenes near a boundary without needing a full visual fingerprint.
        step = max(80.0, window * 1.25)
        count = min(18, max(10, int(source_duration / step) + 1))
        starts = _unique_starts(
            [max_start * i / max(1, count - 1) for i in range(count)],
            max_start,
        )

        # Telegram/Termux is already doing chunk-level parallel reads. Keep
        # window-level concurrency deliberately small because find_engine.py
        # can run up to four scenes at the same time.
        coarse = await _parallel_search_windows(
            server.url,
            starts,
            target_groups,
            usable_regions,
            window,
            source_duration,
            job_dir,
            fps=0.5,
            concurrency=2,
        )

        if not coarse:
            return None

        # Refine only the best distinct coarse hits. Reuse the same range server
        # instead of reopening Telegram for every candidate.
        refined = []
        refine_results = _distinct_results(
            coarse,
            distance=max(8.0, target_duration * 0.7),
        )[:3]

        refine_tasks = []
        for coarse_result in refine_results:
            coarse_center = float(coarse_result["source_time"])
            refine_start = max(0.0, coarse_center - 14.0)
            refine_duration = min(
                source_duration - refine_start,
                max(22.0, target_duration * 1.7 + 8.0),
            )
            refine_tasks.append(
                (
                    refine_start,
                    refine_duration,
                )
            )

        async def refine_one(item):
            refine_start, refine_duration = item
            try:
                return await _search_window(
                    server.url,
                    target_groups,
                    usable_regions,
                    refine_start,
                    max(1.0, refine_duration),
                    job_dir,
                    fps=2.0,
                )
            except Exception as exc:
                logger = __import__("logging").getLogger("visual-matcher")
                logger.info("visual refine failed start=%s: %s", refine_start, exc)
                return None

        refined = [
            result
            for result in await asyncio.gather(
                *(refine_one(item) for item in refine_tasks)
            )
            if result
        ]

        pool = refined or coarse
        candidates = []
        for result in _distinct_results(
            pool,
            distance=max(6.0, target_duration * 0.5),
        ):
            center = float(result["source_time"])
            margin = max(7.0, min(15.0, target_duration * 0.65))
            start = max(0.0, center - margin)
            end = min(source_duration, center + margin + target_duration)
            candidates.append({
                "start": start,
                "end": max(start + 1.0, end),
                "center": center,
                "score": float(result["score"]),
                "median_distance": float(result["median_distance"]),
                "progression": float(result["progression"]),
                "coverage": float(result.get("coverage", 0.0)),
            })

        candidates.sort(key=lambda x: x["score"])
        if not candidates:
            return None
        return candidates[0] | {"candidates": candidates}
    finally:
        if server is not None:
            await server.close()
