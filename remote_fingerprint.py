import asyncio
import base64
import json
import math
from pathlib import Path

import numpy as np

from config import FFMPEG_BIN, TEMP_DIR
from findclip_engine import _encode_hashes, visual_hash
from telegram_remote import open_telegram_range_server


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
    Build a compact visual fingerprint directly from the Telegram range server.

    The complete episode is never saved locally. FFmpeg decodes sampled frames
    from the remote range URL, and only the compact hash arrays are retained.
    """
    server = await open_telegram_range_server(client, source_url)
    work_dir = Path(TEMP_DIR) / "fingerprint_frames"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        duration = float(server.duration)
        if duration <= 0:
            raise RuntimeError("Episode duration unavailable.")

        expected = max(1, int(math.ceil(duration / sample_every)))

        vf = (
            f"fps=1/{float(sample_every):.6f},"
            "scale=160:90:flags=bilinear,"
            "format=gray"
        )
        frame_bytes = 160 * 90

        process = await asyncio.create_subprocess_exec(
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            "-i", server.url,
            "-vf", vf,
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        hashes = []
        times = []
        index = 0

        while True:
            raw = await process.stdout.readexactly(frame_bytes) if process.stdout else b""
            if not raw:
                break

            arr = np.frombuffer(raw, dtype=np.uint8).reshape((90, 160))
            mean = float(arr.mean())
            std = float(arr.std())
            if std < 1.0:
                std = 1.0
            normalized = np.clip(
                ((arr.astype(np.float32) - mean) / std) * 32.0 + 128.0,
                0,
                255,
            ).astype(np.uint8)

            # visual_hash expects an image file, but the same 16x9 normalized
            # representation is produced here without writing every frame.
            small = normalized.reshape(9, 10, 16, 10).mean(axis=(1, 3))
            small = np.clip(small, 0, 255).astype(np.uint8)
            hashes.append(small)
            timestamp = min(
                index * sample_every,
                max(0.0, duration - 0.05),
            )
            times.append(round(float(timestamp), 3))
            index += 1

            if progress:
                try:
                    await progress(index, expected)
                except Exception:
                    pass

        stderr = b""
        if process.stderr:
            stderr = await process.stderr.read()
        return_code = await process.wait()

        if return_code != 0 and not hashes:
            detail = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"FFmpeg fingerprint scan failed: {detail}")

        if not hashes:
            raise RuntimeError("No usable frames were extracted.")

        encoded, shape = _encode_hashes(hashes)
        return {
            "version": 3,
            "format": "compact_uint8_zlib_base64",
            "type": "episode_visual_fingerprint",
            "anime": anime,
            "season": int(season),
            "episode": int(episode),
            "duration": round(duration, 3),
            "sample_every": float(sample_every),
            "shape": shape,
            "times": times,
            "hashes": encoded,
            "source_url": source_url,
        }
    finally:
        await server.close()
        for path in work_dir.glob("*"):
            try:
                path.unlink()
            except Exception:
                pass
        try:
            work_dir.rmdir()
        except Exception:
            pass
