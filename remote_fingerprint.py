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
from telethon.errors import FileReferenceExpiredError


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



DOWNLOAD_CHUNK_BYTES = 512 * 1024
DOWNLOAD_CHUNK_TIMEOUT = 30.0
MAX_DOWNLOAD_RETRIES = 2
DOWNLOAD_RETRY_DELAY = 2.0


async def _download_episode_resumable(
    client,
    message,
    source_url,
    temp_path,
):
    """Download a Telegram episode into a resumable .part file.

    Only complete chunks are appended. If the network disappears, the partial
    file is kept and the next fingerprint run resumes from its exact size.
    The final file is atomically renamed only after the expected byte count is
    reached, so a partial episode can never be mistaken for a complete one.
    """
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if document is None:
        raise RuntimeError("Telegram source message me downloadable document nahi hai.")

    total_size = int(getattr(document, "size", 0) or 0)
    if total_size <= 0:
        raise RuntimeError("Telegram episode file size nahi mila.")

    part_path = Path(str(temp_path) + ".part")
    meta_path = Path(str(part_path) + ".json")

    metadata = {
        "version": 1,
        "source_url": source_url,
        "expected_size": total_size,
        "message_id": int(getattr(message, "id", 0) or 0),
    }

    # Never resume bytes belonging to a different Telegram source/file.
    if part_path.exists():
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            old = None
        if (
            not isinstance(old, dict)
            or old.get("source_url") != source_url
            or int(old.get("expected_size", -1)) != total_size
            or int(old.get("message_id", -1)) != int(getattr(message, "id", 0) or 0)
        ):
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

    if not part_path.exists():
        meta_path.write_text(
            json.dumps(metadata, separators=(",", ":")),
            encoding="utf-8",
        )
        with part_path.open("wb"):
            pass
    elif not meta_path.exists():
        # An untracked partial file is unsafe to resume because its source is
        # unknown. Restart it cleanly rather than risking mixed bytes.
        part_path.unlink(missing_ok=True)
        meta_path.write_text(
            json.dumps(metadata, separators=(",", ":")),
            encoding="utf-8",
        )
        with part_path.open("wb"):
            pass

    current = part_path.stat().st_size
    if current > total_size:
        part_path.unlink(missing_ok=True)
        current = 0
        with part_path.open("wb"):
            pass

    while current < total_size:
        last_error = None

        for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
            try:
                # Refresh the Telegram message for every retry. This also gives
                # Telethon a fresh file reference after expiration.
                if attempt > 1:
                    refreshed = await client.get_messages(
                        getattr(message, "chat_id", None),
                        ids=message.id,
                    )
                    if refreshed:
                        message = refreshed
                        media = getattr(message, "media", None)
                        document = getattr(media, "document", None)
                        if document is None:
                            raise RuntimeError("Refreshed Telegram media missing.")
                        refreshed_size = int(getattr(document, "size", 0) or 0)
                        if refreshed_size and refreshed_size != total_size:
                            raise RuntimeError(
                                f"Telegram file size changed during download "
                                f"({total_size} -> {refreshed_size})."
                            )

                current = part_path.stat().st_size
                iterator = client.iter_download(
                    message.media,
                    offset=current,
                    request_size=DOWNLOAD_CHUNK_BYTES,
                    chunk_size=DOWNLOAD_CHUNK_BYTES,
                )

                while current < total_size:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=DOWNLOAD_CHUNK_TIMEOUT,
                    )
                    if not chunk:
                        raise RuntimeError(
                            f"Telegram download stopped at {current}/{total_size} bytes."
                        )

                    remaining = total_size - current
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]

                    # Append only after a complete Telegram chunk is received.
                    # A network interruption therefore leaves a safe resume point.
                    with part_path.open("ab") as handle:
                        handle.write(chunk)
                        handle.flush()

                    current += len(chunk)

                if current == total_size:
                    break

            except StopAsyncIteration:
                current = part_path.stat().st_size
                if current >= total_size:
                    break
                last_error = RuntimeError(
                    f"Telegram download ended early at {current}/{total_size} bytes."
                )
            except FileReferenceExpiredError as exc:
                last_error = exc
                try:
                    refreshed = await client.get_messages(
                        getattr(message, "chat_id", None),
                        ids=message.id,
                    )
                    if refreshed:
                        message = refreshed
                except Exception:
                    pass
            except asyncio.TimeoutError as exc:
                last_error = RuntimeError(
                    f"Episode download chunk stalled for {DOWNLOAD_CHUNK_TIMEOUT:.0f}s."
                )
            except Exception as exc:
                last_error = exc

            if part_path.exists():
                current = part_path.stat().st_size
            if current >= total_size:
                break

            if attempt < MAX_DOWNLOAD_RETRIES:
                await asyncio.sleep(DOWNLOAD_RETRY_DELAY)

        if current >= total_size:
            break

        detail = str(last_error or "unknown download error")
        raise RuntimeError(
            "RETRY_LIMIT_REACHED: "
            f"Episode download stopped at {current}/{total_size} bytes "
            f"after {MAX_DOWNLOAD_RETRIES} retries. {detail}"
        )

    if part_path.stat().st_size != total_size:
        raise RuntimeError(
            f"Episode download incomplete: {part_path.stat().st_size}/{total_size} bytes."
        )

    # Atomic rename: only the fully downloaded file gets the .episode name.
    part_path.replace(temp_path)
    meta_path.unlink(missing_ok=True)
    return temp_path, total_size

async def build_remote_fingerprint(
    client,
    source_url,
    anime,
    season,
    episode,
    sample_every=0.1,
    progress=None,
    keep_temp=False,
):
    """
    Build a detailed visual fingerprint from the complete Telegram episode.

    Reliability-first mode: the episode is downloaded once to a temporary file,
    then FFmpeg scans the local file sequentially. This avoids repeated remote
    HTTP seeking, Telegram range stalls, and file-reference/range-server issues.
    The temporary episode is deleted after the fingerprint is built.

    Default sampling is every 0.1 seconds, giving 10 samples per second for
    fine-grained temporal matching.
    """
    checkpoint = _checkpoint_path(anime, season, episode)
    temp_path = None
    fingerprint_complete = False

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

        # Reuse a complete temporary download if a previous fingerprint run
        # finished downloading but was interrupted during local FFmpeg scanning.
        if temp_path.exists() and temp_path.stat().st_size >= 1024:
            if progress:
                try:
                    await progress(0, expected_total)
                except Exception:
                    pass
        else:
            if progress:
                try:
                    await progress(0, expected_total)
                except Exception:
                    pass
            await _download_episode_resumable(
                client,
                message,
                source_url,
                temp_path,
            )

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
        if keep_temp:
            result["_temp_episode_path"] = str(temp_path)
        fingerprint_complete = True
        return result
    finally:
        # Successful fingerprints release the full temporary episode. If local
        # fingerprinting is interrupted, keep the completed episode so the next
        # run can skip the network download; resumable .part files are kept by
        # the downloader itself after network failures.
        if fingerprint_complete and temp_path is not None and not keep_temp:
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


def build_video_index_image(video_path, output_path, every_seconds=2.0, columns=12):
    """Build a human-viewable contact sheet from real episode video frames.

    The source episode is only read from a temporary local file and the
    generated JPEG is the only visual artifact kept for Telegram upload.
    """
    from PIL import Image, ImageDraw
    import subprocess
    import tempfile

    video_path = str(video_path)
    every_seconds = max(0.5, float(every_seconds))
    columns = max(1, int(columns))

    with tempfile.TemporaryDirectory(prefix="episode_index_") as tmp:
        tmp_path = Path(tmp)
        pattern = str(tmp_path / "frame_%06d.jpg")
        args = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "error",
            "-i", video_path,
            "-vf", f"fps=1/{every_seconds:.6f},scale=160:90:flags=lanczos",
            "-q:v", "5",
            pattern,
        ]
        result = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                f"Index image FFmpeg failed: {detail or 'unknown error'}"
            )

        files = sorted(tmp_path.glob("frame_*.jpg"))
        if not files:
            raise RuntimeError("Index image me koi real video frame nahi mila.")

        thumb_w, thumb_h = 160, 90
        label_h = 20
        rows = int(math.ceil(len(files) / columns))
        canvas = Image.new(
            "RGB",
            (columns * thumb_w, rows * (thumb_h + label_h)),
            "white",
        )
        draw = ImageDraw.Draw(canvas)

        for idx, frame_file in enumerate(files):
            with Image.open(frame_file) as image:
                image = image.convert("RGB")
                x = (idx % columns) * thumb_w
                y = (idx // columns) * (thumb_h + label_h)
                canvas.paste(image, (x, y))

            seconds = idx * every_seconds
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            draw.rectangle(
                (x, y + thumb_h, x + thumb_w, y + thumb_h + label_h),
                fill="white",
            )
            draw.text(
                (x + 3, y + thumb_h + 2),
                f"{mins:02d}:{secs:02d}",
                fill="black",
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="JPEG", quality=82, optimize=True)
        return output_path
