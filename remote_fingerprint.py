import asyncio
import base64
import json
import math
import re
import zlib
from pathlib import Path

import numpy as np

from config import FFMPEG_BIN, TEMP_DIR
from telegram_media import parse_telegram_message_link


CHECKPOINT_DIR = Path(TEMP_DIR) / "fingerprint_checkpoints"
CHECKPOINT_INTERVAL = 20
FRAME_READ_TIMEOUT = 60.0
SEGMENT_SAMPLES = 12
SEGMENT_CONCURRENCY = 2
RETRY_DELAY = 2.0
MAX_RETRIES_PER_SEGMENT = 2


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
    sample_every=2.0,
    progress=None,
):
    """
    Build a detailed visual fingerprint from the complete Telegram episode.

    Reliability-first mode: the episode is downloaded once to a temporary file,
    then FFmpeg scans the local file sequentially. This avoids repeated remote
    HTTP seeking, Telegram range stalls, and file-reference/range-server issues.
    The temporary episode is deleted after the fingerprint is built.

    Default sampling is every 2 seconds, giving a much denser timeline than the
    old 8-second remote fingerprint and making scene matching more precise.
    """
    checkpoint = _checkpoint_path(anime, season, episode)
    temp_path = None

    try:
        chat, message_id = parse_telegram_message_link(source_url)
        message = await client.get_messages(chat, ids=message_id)
        if not message:
            raise RuntimeError(
                f"Telegram source message nahi mila (chat={chat}, message={message_id})."
            )
        if getattr(message, "media", None) is None:
            raise RuntimeError("Telegram source message me media nahi hai.")

        document = getattr(message.media, "document", None)
        duration = None
        for attribute in getattr(document, "attributes", []) or []:
            value = getattr(attribute, "duration", None)
            if value:
                duration = float(value)
                break
        if not duration or duration <= 0:
            raise RuntimeError("Telegram source video duration nahi mila.")

        expected_total = max(1, int(math.ceil(duration / float(sample_every))))
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = CHECKPOINT_DIR / (
            f"{_safe_name(anime)}_S{int(season)}E{int(episode)}.episode"
        )

        # Reuse a complete temporary download if a previous fingerprint run was
        # interrupted after the download finished.
        if not temp_path.exists() or temp_path.stat().st_size < 1024:
            if progress:
                try:
                    await progress(0, expected_total)
                except Exception:
                    pass
            await client.download_media(message, file=str(temp_path))

        if not temp_path.exists() or temp_path.stat().st_size < 1024:
            raise RuntimeError("Episode download incomplete or empty.")

        vf = (
            f"fps=1/{float(sample_every):.6f},"
            "scale=160:90:flags=bilinear,"
            "format=gray"
        )
        frame_bytes = 160 * 90
        args = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            "-threads", "0",
            "-i", str(temp_path),
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

        hashes = []
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        process.stdout.readexactly(frame_bytes),
                        timeout=FRAME_READ_TIMEOUT,
                    )
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        raise RuntimeError(
                            f"Fingerprint FFmpeg returned a partial frame "
                            f"({len(exc.partial)}/{frame_bytes} bytes)."
                        ) from exc
                    break
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"Fingerprint FFmpeg stalled for {FRAME_READ_TIMEOUT:.0f}s."
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

                if progress and (len(hashes) % CHECKPOINT_INTERVAL == 0):
                    try:
                        await progress(len(hashes), expected_total)
                    except Exception:
                        pass

            stderr = await process.stderr.read()
            return_code = await process.wait()
            if return_code != 0:
                detail = stderr.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(
                    f"Fingerprint FFmpeg failed: {detail or 'unknown error'}"
                )
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

        if not hashes:
            raise RuntimeError("Fingerprint produced no video samples.")

        times = [
            round(
                min(index * float(sample_every), max(0.0, duration - 0.05)),
                3,
            )
            for index in range(len(hashes))
        ]

        # Keep the fingerprint compact; the full episode itself is temporary.
        arr = np.asarray(hashes, dtype=np.uint8)
        compressed = zlib.compress(arr.tobytes(), level=9)
        encoded = base64.b64encode(compressed).decode("ascii")
        result = {
            "version": 5,
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

        if progress:
            try:
                await progress(len(hashes), len(hashes))
            except Exception:
                pass

        checkpoint.unlink(missing_ok=True)
        return result
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


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
