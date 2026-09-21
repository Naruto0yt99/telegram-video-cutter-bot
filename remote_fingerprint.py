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
FRAME_READ_TIMEOUT = 120.0
RETRY_DELAY = 3.0


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


async def build_remote_fingerprint(
    client,
    source_url,
    anime,
    season,
    episode,
    sample_every=2.0,
    progress=None,
):
    """
    Build a compact visual fingerprint directly from Telegram.

    The complete episode is never saved locally. The scan keeps the requested
    2-second sampling, but it is resumable and self-healing: checkpoints are
    written every 20 samples, remote range stalls are timed out, FFmpeg is
    restarted from the last checkpoint, and completed checkpoints are deleted
    only after the final fingerprint is safely returned.
    """
    checkpoint = _checkpoint_path(anime, season, episode)
    hashes = []
    times = []
    server = None

    try:
        while True:
            server = await open_telegram_range_server(client, source_url)
            duration = float(server.duration)
            if duration <= 0:
                raise RuntimeError("Episode duration unavailable.")

            if not hashes:
                hashes, times = _load_checkpoint(
                    checkpoint, source_url, duration, float(sample_every)
                )

            completed = len(hashes)
            if completed >= max(1, int(math.ceil(duration / sample_every))):
                break

            resume_time = (
                min(
                    duration - 0.05,
                    max(0.0, (times[-1] + sample_every)),
                )
                if times
                else 0.0
            )
            remaining = max(0.05, duration - resume_time)
            expected_total = max(1, int(math.ceil(duration / sample_every)))
            expected_remaining = max(1, expected_total - completed)

            vf = (
                f"fps=1/{float(sample_every):.6f},"
                "scale=160:90:flags=bilinear,"
                "format=gray"
            )
            frame_bytes = 160 * 90

            process = None
            try:
                args = [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-threads", "1",
                ]
                if resume_time > 0.05:
                    args += ["-ss", f"{resume_time:.3f}"]
                args += [
                    "-i", server.url,
                    "-vf", vf,
                    "-f", "rawvideo",
                    "-pix_fmt", "gray",
                    "-",
                ]

                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                local_index = 0
                while True:
                    if not process.stdout:
                        break
                    try:
                        raw = await asyncio.wait_for(
                            process.stdout.readexactly(frame_bytes),
                            timeout=FRAME_READ_TIMEOUT,
                        )
                    except asyncio.IncompleteReadError:
                        break
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            f"FFmpeg/Telegram stream stalled for {FRAME_READ_TIMEOUT:.0f}s."
                        )

                    if not raw:
                        break

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
                    timestamp = min(
                        resume_time + local_index * sample_every,
                        max(0.0, duration - 0.05),
                    )
                    times.append(round(float(timestamp), 3))
                    local_index += 1

                    if len(hashes) % CHECKPOINT_INTERVAL == 0:
                        _save_checkpoint(
                            checkpoint, source_url, anime, season, episode,
                            duration, sample_every, hashes, times,
                        )

                    if progress:
                        try:
                            await progress(len(hashes), expected_total)
                        except Exception:
                            pass

                stderr = b""
                if process.stderr:
                    stderr = await process.stderr.read()
                return_code = await process.wait()

                if return_code != 0:
                    detail = stderr.decode("utf-8", errors="ignore").strip()
                    raise RuntimeError(
                        f"FFmpeg fingerprint scan failed: {detail or 'unknown FFmpeg error'}"
                    )

                if not hashes:
                    raise RuntimeError("No usable frames were extracted.")

                if len(hashes) < expected_total:
                    # Normal EOF can be a few frames short. If it is materially
                    # short, restart from the last checkpoint instead of silently
                    # accepting an incomplete fingerprint.
                    if len(hashes) <= completed:
                        raise RuntimeError("Fingerprint scan made no progress.")
                    _save_checkpoint(
                        checkpoint, source_url, anime, season, episode,
                        duration, sample_every, hashes, times,
                    )
                    continue

                break

            except Exception:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                if process is not None:
                    try:
                        await process.wait()
                    except Exception:
                        pass

                _save_checkpoint(
                    checkpoint, source_url, anime, season, episode,
                    duration, sample_every, hashes, times,
                )
                await asyncio.sleep(RETRY_DELAY)
            finally:
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await process.wait()
                    except Exception:
                        pass
                if server is not None:
                    await server.close()
                    server = None

            # If we reached here after an exception/partial EOF, reopen the
            # Telegram range server and resume from the last durable checkpoint.
            if len(hashes) >= expected_total:
                break

        arr = np.asarray(hashes, dtype=np.uint8)
        compressed = zlib.compress(arr.tobytes(), level=9)
        encoded = base64.b64encode(compressed).decode("ascii")
        result = {
            "version": 3,
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
