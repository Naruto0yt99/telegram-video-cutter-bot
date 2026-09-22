import asyncio
import base64
import json
import math
import re
import zlib
from pathlib import Path

import numpy as np

from config import FFMPEG_BIN, TEMP_DIR
from telegram_remote import open_telegram_range_server


CHECKPOINT_DIR = Path(TEMP_DIR) / "fingerprint_checkpoints"
CHECKPOINT_INTERVAL = 20
FRAME_READ_TIMEOUT = 60.0
SEGMENT_SAMPLES = 12
SEGMENT_CONCURRENCY = 2
RETRY_DELAY = 2.0
MAX_RETRIES_PER_SEGMENT = 3


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._-") or "episode"


def _checkpoint_path(anime, season, episode):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"{_safe_name(anime)}_S{int(season)}E{int(episode)}.json"


def _load_checkpoint(path, source_url, duration, sample_every):
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("source_url") != source_url:
            return [], []
        if abs(float(data.get("duration", 0.0)) - duration) > 1.0:
            return [], []
        if abs(float(data.get("sample_every", 0.0)) - sample_every) > 0.001:
            return [], []
        hashes = [
            np.asarray(item, dtype=np.uint8)
            for item in data.get("hashes", [])
        ]
        times = [float(x) for x in data.get("times", [])]
        if len(hashes) != len(times):
            return [], []
        return hashes, times
    except Exception:
        return [], []


def _save_checkpoint(path, source_url, anime, season, episode, duration, sample_every, hashes, times):
    payload = {
        "version": 1,
        "source_url": source_url,
        "anime": anime,
        "season": int(season),
        "episode": int(episode),
        "duration": round(duration, 3),
        "sample_every": float(sample_every),
        "hashes": [np.asarray(item, dtype=np.uint8).tolist() for item in hashes],
        "times": times,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


async def _fingerprint_segment(
    server_url,
    start_time,
    sample_count,
    sample_every,
    duration,
    segment_id,
):
    """Decode one bounded sampling segment and return (times, hashes)."""
    vf = (
        f"fps=1/{float(sample_every):.6f},"
        "scale=160:90:flags=bilinear,"
        "format=gray"
    )
    frame_bytes = 160 * 90
    segment_duration = max(
        0.75,
        min(
            max(0.75, duration - start_time),
            (max(1, sample_count - 1) * float(sample_every)) + 0.75,
        ),
    )

    args = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-threads", "0",
        "-ss", f"{max(0.0, start_time):.3f}",
        "-i", server_url,
        "-t", f"{segment_duration:.3f}",
        "-vf", vf,
        "-frames:v", str(max(1, int(sample_count))),
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-",
    ]

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    hashes = []
    try:
        for local_index in range(max(1, int(sample_count))):
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readexactly(frame_bytes),
                    timeout=FRAME_READ_TIMEOUT,
                )
            except asyncio.IncompleteReadError as exc:
                raise RuntimeError(
                    f"Segment {segment_id} ended after {len(hashes)}/{sample_count} samples."
                ) from exc
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"Segment {segment_id} stalled for {FRAME_READ_TIMEOUT:.0f}s."
                ) from exc

            arr = np.frombuffer(raw, dtype=np.uint8).reshape((90, 160))
            mean = float(arr.mean())
            std = max(1.0, float(arr.std()))
            normalized = np.clip(
                ((arr.astype(np.float32) - mean) / std) * 32.0 + 128.0,
                0,
                255,
            ).astype(np.uint8)
            small = normalized.reshape(9, 10, 16, 10).mean(axis=(1, 3))
            hashes.append(np.clip(small, 0, 255).astype(np.uint8))

        stderr = await process.stderr.read()
        return_code = await process.wait()
        if return_code != 0:
            detail = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                f"Segment {segment_id} FFmpeg failed: {detail or 'unknown error'}"
            )

        times = [
            round(
                min(
                    start_time + index * sample_every,
                    max(0.0, duration - 0.05),
                ),
                3,
            )
            for index in range(len(hashes))
        ]
        return times, hashes
    except Exception:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        except Exception:
            pass
        raise


async def build_remote_fingerprint(
    client,
    source_url,
    anime,
    season,
    episode,
    sample_every=8.0,
    progress=None,
):
    """
    Build a compact visual fingerprint directly from Telegram.

    The episode is never downloaded as one local file. Sampling is divided
    into small bounded FFmpeg segments and two segments are processed in
    parallel. Each segment has an explicit frame limit, so the final few
    samples cannot hang while waiting for an EOF from a remote Telegram
    range stream. A small checkpoint remains only for crash-resume safety;
    the completed fingerprint and visual index are uploaded to Telegram.
    """
    checkpoint = _checkpoint_path(anime, season, episode)
    hashes = []
    times = []
    server = None

    try:
        server = await open_telegram_range_server(client, source_url)
        duration = float(server.duration)
        if duration <= 0:
            raise RuntimeError("Episode duration unavailable.")

        expected_total = max(1, int(math.ceil(duration / float(sample_every))))

        saved_hashes, saved_times = _load_checkpoint(
            checkpoint, source_url, duration, float(sample_every)
        )
        if saved_hashes and len(saved_hashes) == len(saved_times):
            hashes = saved_hashes[:expected_total]
            times = saved_times[:expected_total]

        # Build an explicit sample timeline. This avoids the old
        # "decode one huge remote stream and hope EOF arrives" behavior.
        sample_times = [
            round(min(index * float(sample_every), max(0.0, duration - 0.05)), 3)
            for index in range(expected_total)
        ]

        existing = {
            round(float(t), 3): h
            for t, h in zip(times, hashes)
            if h is not None
        }
        times = []
        hashes = []

        pending_indices = [
            index
            for index, sample_time in enumerate(sample_times)
            if round(sample_time, 3) not in existing
        ]

        async def save_progress():
            if not progress:
                return
            try:
                await progress(len(existing), expected_total)
            except Exception:
                pass

        await save_progress()

        semaphore = asyncio.Semaphore(SEGMENT_CONCURRENCY)

        async def run_segment(segment_id, indices):
            async with semaphore:
                start_time = sample_times[indices[0]]
                count = len(indices)
                last_error = None
                for attempt in range(1, MAX_RETRIES_PER_SEGMENT + 1):
                    try:
                        segment_times, segment_hashes = await _fingerprint_segment(
                            server.url,
                            start_time,
                            count,
                            float(sample_every),
                            duration,
                            segment_id,
                        )
                        if len(segment_hashes) != count:
                            raise RuntimeError(
                                f"Segment {segment_id} returned "
                                f"{len(segment_hashes)}/{count} samples."
                            )
                        return indices, segment_times, segment_hashes
                    except Exception as exc:
                        last_error = exc
                        if attempt < MAX_RETRIES_PER_SEGMENT:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                raise RuntimeError(
                    f"Fingerprint segment {segment_id} failed after "
                    f"{MAX_RETRIES_PER_SEGMENT} attempts: {last_error}"
                )

        segments = [
            pending_indices[offset:offset + SEGMENT_SAMPLES]
            for offset in range(0, len(pending_indices), SEGMENT_SAMPLES)
        ]

        for offset in range(0, len(segments), SEGMENT_CONCURRENCY):
            batch = segments[offset:offset + SEGMENT_CONCURRENCY]
            results = await asyncio.gather(
                *(
                    run_segment(
                        offset + local_id + 1,
                        segment_indices,
                    )
                    for local_id, segment_indices in enumerate(batch)
                )
            )

            for indices, segment_times, segment_hashes in results:
                for index, sample_time, sample_hash in zip(
                    indices, segment_times, segment_hashes
                ):
                    existing[round(sample_time, 3)] = sample_hash

            ordered_times = []
            ordered_hashes = []
            for sample_time in sample_times:
                key = round(sample_time, 3)
                if key in existing:
                    ordered_times.append(sample_time)
                    ordered_hashes.append(existing[key])

            times = ordered_times
            hashes = ordered_hashes

            if len(hashes) % CHECKPOINT_INTERVAL == 0 or len(hashes) == expected_total:
                _save_checkpoint(
                    checkpoint, source_url, anime, season, episode,
                    duration, sample_every, hashes, times,
                )
            await save_progress()

        if len(hashes) != expected_total:
            missing = expected_total - len(hashes)
            raise RuntimeError(
                f"Fingerprint incomplete: {len(hashes)}/{expected_total}; "
                f"{missing} samples missing."
            )

        arr = np.asarray(hashes, dtype=np.uint8)
        compressed = zlib.compress(arr.tobytes(), level=9)
        encoded = base64.b64encode(compressed).decode("ascii")
        result = {
            "version": 4,
            "format": "compact_uint8_zlib_base64",
            "type": "episode_visual_fingerprint",
            "anime": anime,
            "season": int(season),
            "episode": int(episode),
            "duration": round(duration, 3),
            "sample_every": float(sample_every),
            "shape": list(arr.shape),
            "times": times,
            "hashes": encoded,
            "source_url": source_url,
        }
        checkpoint.unlink(missing_ok=True)
        return result
    finally:
        if server is not None:
            await server.close()


def build_fingerprint_index_image(fingerprint, output_path, every_seconds=10.0, columns=12):
    """Render a compact visual index from stored fingerprint samples."""
    from PIL import Image, ImageDraw, ImageFont

    hashes = fingerprint.get("hashes")
    if not hashes:
        raise ValueError("Fingerprint has no hashes.")

    raw = zlib.decompress(base64.b64decode(str(hashes).encode("ascii")))
    shape = tuple(int(x) for x in fingerprint.get("shape", []))
    if len(shape) != 3:
        raise ValueError("Invalid fingerprint shape.")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(shape)
    times = [float(x) for x in fingerprint.get("times", [])]
    sample_every = float(fingerprint.get("sample_every", 2.0) or 2.0)
    step = max(1, int(round(float(every_seconds) / sample_every)))
    selected = list(range(0, len(arr), step))
    if selected and selected[-1] != len(arr) - 1:
        selected.append(len(arr) - 1)
    if not selected:
        selected = [0]

    thumb_w, thumb_h = 160, 90
    label_h = 18
    columns = max(1, int(columns))
    rows = int(math.ceil(len(selected) / columns))
    canvas = Image.new("RGB", (columns * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(canvas)
    for pos, idx in enumerate(selected):
        gray = Image.fromarray(arr[idx], mode="L").resize((thumb_w, thumb_h), Image.Resampling.BILINEAR)
        tile = Image.merge("RGB", (gray, gray, gray))
        x = (pos % columns) * thumb_w
        y = (pos // columns) * (thumb_h + label_h)
        canvas.paste(tile, (x, y))
        seconds = times[idx] if idx < len(times) else idx * sample_every
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        draw.rectangle((x, y + thumb_h, x + thumb_w, y + thumb_h + label_h), fill="white")
        draw.text((x + 3, y + thumb_h + 2), f"{mins:02d}:{secs:02d}", fill="black")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="JPEG", quality=88, optimize=True)
    return output_path
