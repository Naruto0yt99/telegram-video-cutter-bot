import asyncio
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from config import FFMPEG_BIN, TEMP_DIR
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
    if len(regions) > 1 or regions[0] != (0.0, 0.0, 1.0, 1.0):
        signatures.append(_signature(image))
    return signatures


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
        "-vf", f"fps={max(0.2, float(fps))},scale=256:144:force_original_aspect_ratio=decrease,pad=256:144:(ow-iw)/2:(oh-ih)/2",
        "-q:v", "5",
        str(pattern),
    ]
    await run_command(*args)
    return sorted(out_dir.glob("frame_*.jpg"))


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
    score = median_dist * 0.65 + p25 * 0.15 + (1.0 - progression) * 0.20
    return {
        "score": score,
        "median_distance": median_dist,
        "progression": progression,
        "indices": indices,
        "source_time": source_times[indices[len(indices) // 2]],
    }


async def _load_signatures(frame_paths, usable_regions=None):
    groups = []
    for path in frame_paths:
        try:
            groups.append(_frame_signatures(path, usable_regions))
        except Exception:
            continue
    return groups


async def _search_window(server_url, target_groups, start, duration, root, fps=1.0):
    source_dir = root / f"source_{int(start * 10)}"
    source_frames = await _extract_frames(
        server_url, source_dir, fps=fps, start=start, duration=duration, remote=True
    )
    source_groups = await _load_signatures(source_frames)
    if not source_groups:
        return None
    source_times = [start + i / fps for i in range(len(source_groups))]
    return _sequence_score(target_groups, source_groups, source_times)


async def find_visual_match(client, candidate, segment, target_video, job_dir, source_duration):
    """Find a source timestamp using local visual retrieval; no candidate Gemini upload."""
    from telegram_remote import open_telegram_range_server

    target_dir = job_dir / f"target_frames_{int(float(segment['start_time']) * 10)}"
    target_frames = await _extract_frames(target_video, target_dir, fps=1.0)
    if not target_frames:
        return None
    target_groups = await _load_signatures(target_frames, segment.get("usable_regions"))
    if not target_groups:
        return None

    hint = segment.get("source_start_hint")
    try:
        hint = None if hint is None else float(hint)
    except (TypeError, ValueError):
        hint = None
    if hint is not None:
        hint = max(0.0, min(hint, max(0.0, source_duration - 1.0)))

    duration = max(1.0, float(segment["end_time"]) - float(segment["start_time"]))
    window = max(30.0, min(75.0, duration * 4.0 + 15.0))
    starts = []
    if hint is not None:
        for delta in (0, -30, 30, -60, 60, -120, 120, -180, 180):
            starts.append(max(0.0, min(hint + delta, max(0.0, source_duration - window))))
    else:
        step = max(20.0, window * 0.8)
        count = min(36, max(8, int(source_duration / step) + 1))
        max_start = max(0.0, source_duration - window)
        starts.extend(max_start * i / max(1, count - 1) for i in range(count))

    starts = list(dict.fromkeys(round(x, 1) for x in starts))
    best = None
    server = None
    try:
        server = await open_telegram_range_server(client, candidate["source_url"])
        for start in starts:
            try:
                result = await _search_window(
                    server.url,
                    target_groups,
                    start,
                    min(window, max(1.0, source_duration - start)),
                    job_dir,
                    fps=1.0,
                )
            except Exception as exc:
                continue
            if result and (best is None or result["score"] < best["score"]):
                best = result
    finally:
        if server is not None:
            await server.close()

    if not best:
        return None

    center = float(best["source_time"])
    margin = max(8.0, min(18.0, duration * 0.75))
    start = max(0.0, center - margin)
    end = min(source_duration, center + margin + duration)
    return {
        "start": start,
        "end": max(start + 1.0, end),
        "center": center,
        "score": float(best["score"]),
        "median_distance": float(best["median_distance"]),
        "progression": float(best["progression"]),
    }
