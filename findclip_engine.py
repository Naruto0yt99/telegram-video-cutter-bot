import asyncio
import base64
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

from config import DATA_DIR, INDEX_DIR, TEMP_DIR, FFMPEG_BIN, FFPROBE_BIN


SOURCE_DIR = DATA_DIR / "sources"
SOURCE_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# BASIC MEDIA HELPERS
# ---------------------------------------------------------

def duration(path):
    path = str(path)

    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed: {result.stderr.strip()}"
        )

    try:
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def extract_frame(path, timestamp, output):
    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(max(0.0, float(timestamp))),
        "-i", str(path),
        "-frames:v", "1",
        "-vf", "scale=160:90",
        "-y",
        str(output),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return False

    return Path(output).exists()


def visual_hash(image_path):
    """
    Compact visual fingerprint.

    16x9 grayscale image -> uint8 array.
    This is much smaller than storing float16 nested JSON arrays.
    """

    image = Image.open(image_path).convert("L")
    image = image.resize((16, 9), Image.Resampling.BILINEAR)

    arr = np.asarray(image, dtype=np.float32)

    # Normalize brightness so small brightness differences
    # don't destroy matching.
    mean = float(arr.mean())
    std = float(arr.std())

    if std < 1.0:
        std = 1.0

    arr = (arr - mean) / std

    # Convert normalized image into compact uint8 range.
    arr = np.clip((arr * 32.0) + 128.0, 0, 255)
    return arr.astype(np.uint8)


def visual_distance(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)

    if a.shape != b.shape:
        return 999.0

    return float(np.mean(np.abs(a - b)))


# ---------------------------------------------------------
# INDEX ID / PATHS
# ---------------------------------------------------------

def index_name(anime, season, episode):
    raw = f"{anime}|{season}|{episode}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def index_path_for(anime, season, episode):
    return INDEX_DIR / f"{index_name(anime, season, episode)}.json"


def source_path_for(anime, season, episode, extension=".mp4"):
    safe = "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in str(anime)
    )

    season = str(season)
    episode = str(episode)

    extension = extension or ".mp4"
    if not extension.startswith("."):
        extension = "." + extension

    return SOURCE_DIR / f"{safe}_S{season}_E{episode}{extension}"


# ---------------------------------------------------------
# COMPACT INDEX ENCODING
# ---------------------------------------------------------

def _encode_hashes(hashes):
    """
    Store all frame fingerprints as one compressed binary blob.

    Shape:
        number_of_frames x 9 x 16

    Stored as:
        zlib(raw uint8 bytes) -> base64 text
    """

    if not hashes:
        return "", [0, 9, 16]

    arr = np.asarray(hashes, dtype=np.uint8)

    raw = arr.tobytes()
    compressed = zlib.compress(raw, level=9)

    encoded = base64.b64encode(compressed).decode("ascii")

    return encoded, list(arr.shape)


def _decode_hashes(encoded, shape):
    if not encoded:
        return np.empty((0, 9, 16), dtype=np.uint8)

    compressed = base64.b64decode(encoded.encode("ascii"))
    raw = zlib.decompress(compressed)

    arr = np.frombuffer(raw, dtype=np.uint8)

    return arr.reshape(tuple(shape))


# ---------------------------------------------------------
# INDEX BUILDING
# ---------------------------------------------------------

def make_index(
    source_path,
    index_path,
    sample_every=2.0,
    progress=None,
):
    source_path = Path(source_path)
    index_path = Path(index_path)

    if not source_path.exists():
        raise FileNotFoundError(
            f"Source video not found: {source_path}"
        )

    duration_value = duration(source_path)

    if duration_value <= 0:
        raise RuntimeError("Could not determine video duration.")

    work_dir = Path(
        tempfile.mkdtemp(
            prefix="findclip_",
            dir=str(TEMP_DIR),
        )
    )

    hashes = []
    times = []

    try:
        total = max(
            1,
            int(math.ceil(duration_value / sample_every))
        )

        for i in range(total):
            timestamp = min(
                i * sample_every,
                max(0.0, duration_value - 0.05),
            )

            frame_path = work_dir / f"{i:06d}.jpg"

            ok = extract_frame(
                source_path,
                timestamp,
                frame_path,
            )

            if not ok:
                continue

            try:
                fingerprint = visual_hash(frame_path)
            except Exception:
                continue

            hashes.append(fingerprint)
            times.append(round(float(timestamp), 3))

            if progress:
                try:
                    progress(i + 1, total)
                except Exception:
                    pass

        if not hashes:
            raise RuntimeError(
                "No usable video frames were extracted."
            )

        encoded, shape = _encode_hashes(hashes)

        data = {
            "version": 2,
            "format": "compact_uint8_zlib_base64",
            "duration": round(float(duration_value), 3),
            "sample_every": float(sample_every),
            "shape": shape,
            "times": times,
            "hashes": encoded,

            # Intentionally empty.
            # The real source is stored in the library DB.
            "source": "",
        }

        index_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_index = index_path.with_suffix(
            index_path.suffix + ".tmp"
        )

        with open(temp_index, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                separators=(",", ":"),
            )

        temp_index.replace(index_path)

        return data

    finally:
        shutil.rmtree(
            work_dir,
            ignore_errors=True,
        )


def load_index(index_path):
    index_path = Path(index_path)

    with open(index_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Version 2 compact index.
    if data.get("version") == 2:
        shape = data.get(
            "shape",
            [0, 9, 16],
        )

        data["_hash_array"] = _decode_hashes(
            data.get("hashes", ""),
            shape,
        )

        data["_times"] = np.asarray(
            data.get("times", []),
            dtype=np.float32,
        )

        return data

    # Backward compatibility for old indexes.
    if "frames" in data:
        old_frames = data.get("frames", [])

        hashes = []
        times = []

        for frame in old_frames:
            try:
                hashes.append(
                    np.asarray(
                        frame["hash"],
                        dtype=np.float32,
                    )
                )
                times.append(
                    float(frame["time"])
                )
            except Exception:
                continue

        data["_legacy_hashes"] = hashes
        data["_times"] = np.asarray(
            times,
            dtype=np.float32,
        )

    return data


# ---------------------------------------------------------
# QUERY SEARCH
# ---------------------------------------------------------

def _index_frames(index_data):
    """
    Return:
        times, hashes
    """

    if "_hash_array" in index_data:
        return (
            index_data.get(
                "_times",
                np.asarray([], dtype=np.float32),
            ),
            index_data["_hash_array"],
        )

    # Old index support.
    frames = index_data.get("frames", [])

    if frames:
        times = []
        hashes = []

        for frame in frames:
            try:
                times.append(float(frame["time"]))
                hashes.append(
                    np.asarray(
                        frame["hash"],
                        dtype=np.float32,
                    )
                )
            except Exception:
                continue

        return (
            np.asarray(times, dtype=np.float32),
            hashes,
        )

    return (
        np.asarray([], dtype=np.float32),
        [],
    )


def find_visual_candidates(
    query_path,
    index_data,
    sample_every=1.0,
):
    query_path = Path(query_path)

    query_duration = duration(query_path)

    if query_duration <= 0:
        return []

    source_times, source_hashes = _index_frames(
        index_data
    )

    if len(source_times) == 0:
        return []

    work_dir = Path(
        tempfile.mkdtemp(
            prefix="findclip_query_",
            dir=str(TEMP_DIR),
        )
    )

    results = []

    try:
        query_count = max(
            1,
            int(math.ceil(
                query_duration / sample_every
            ))
        )

        for i in range(query_count):
            query_time = min(
                i * sample_every,
                max(0.0, query_duration - 0.05),
            )

            frame_path = work_dir / f"{i:06d}.jpg"

            if not extract_frame(
                query_path,
                query_time,
                frame_path,
            ):
                continue

            try:
                query_hash = visual_hash(frame_path)
            except Exception:
                continue

            best_distance = 999.0
            best_index = -1

            # Compare query frame with source fingerprints.
            if isinstance(source_hashes, np.ndarray):
                q = query_hash.astype(np.float32)
                src = source_hashes.astype(np.float32)

                distances = np.mean(
                    np.abs(src - q),
                    axis=(1, 2),
                )

                best_index = int(
                    np.argmin(distances)
                )

                best_distance = float(
                    distances[best_index]
                )

            else:
                for j, source_hash in enumerate(
                    source_hashes
                ):
                    d = visual_distance(
                        query_hash,
                        source_hash,
                    )

                    if d < best_distance:
                        best_distance = d
                        best_index = j

            if best_index >= 0:
                results.append({
                    "query_time": round(
                        float(query_time),
                        3,
                    ),
                    "source_time": round(
                        float(source_times[best_index]),
                        3,
                    ),
                    "distance": round(
                        best_distance,
                        4,
                    ),
                })

    finally:
        shutil.rmtree(
            work_dir,
            ignore_errors=True,
        )

    return results


# ---------------------------------------------------------
# MATCH ANALYSIS
# ---------------------------------------------------------

def continuous_match(
    results,
    query_duration,
):
    if not results:
        return None

    # Convert visual distance into rough similarity.
    # 0 = identical, larger = less similar.
    distances = np.asarray(
        [
            float(x.get("distance", 999.0))
            for x in results
        ],
        dtype=np.float32,
    )

    source_times = np.asarray(
        [
            float(x.get("source_time", 0.0))
            for x in results
        ],
        dtype=np.float32,
    )

    query_times = np.asarray(
        [
            float(x.get("query_time", 0.0))
            for x in results
        ],
        dtype=np.float32,
    )

    if len(distances) == 0:
        return None

    # Lower distance is better.
    weights = np.maximum(
        0.0,
        1.0 - (distances / 64.0),
    )

    if float(weights.sum()) <= 0:
        best = int(np.argmin(distances))
        return {
            "source_start": float(
                source_times[best]
            ),
            "source_end": float(
                source_times[best]
            ),
            "confidence": 0.0,
            "coverage": 0.0,
            "offset": float(
                source_times[best]
                - query_times[best]
            ),
        }

    offsets = source_times - query_times

    # Find dominant offset using rounded buckets.
    buckets = {}

    for offset, weight in zip(
        offsets,
        weights,
    ):
        bucket = round(
            float(offset),
            1,
        )
        buckets[bucket] = (
            buckets.get(bucket, 0.0)
            + float(weight)
        )

    dominant_offset = max(
        buckets,
        key=buckets.get,
    )

    tolerance = 1.5

    mask = np.abs(
        offsets - dominant_offset
    ) <= tolerance

    good_distances = distances[mask]

    if len(good_distances) == 0:
        return None

    matched_queries = query_times[mask]
    matched_sources = source_times[mask]

    source_start = float(
        matched_sources.min()
    )

    source_end = float(
        matched_sources.max()
    )

    coverage = (
        len(good_distances)
        / max(
            1,
            len(results),
        )
    )

    avg_distance = float(
        np.mean(good_distances)
    )

    visual_score = max(
        0.0,
        100.0 - (
            avg_distance * 1.45
        ),
    )

    confidence = (
        visual_score * 0.70
        + min(100.0, coverage * 100.0)
        * 0.30
    )

    confidence = max(
        0.0,
        min(
            100.0,
            confidence,
        ),
    )

    return {
        "source_start": source_start,
        "source_end": source_end,
        "confidence": round(
            confidence,
            2,
        ),
        "coverage": round(
            coverage,
            4,
        ),
        "offset": round(
            float(dominant_offset),
            3,
        ),
        "matched_frames": int(
            len(good_distances)
        ),
        "query_duration": float(
            query_duration
        ),
    }


# ---------------------------------------------------------
# CLIP CREATION
# ---------------------------------------------------------

def create_clip(
    source,
    start,
    end,
    output,
):
    source = Path(source)
    output = Path(output)

    if not source.exists():
        raise FileNotFoundError(
            f"Source video not found: {source}"
        )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    start = max(
        0.0,
        float(start),
    )

    end = max(
        start + 0.05,
        float(end),
    )

    duration_value = end - start

    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(duration_value),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-y",
        str(output),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg clip failed: "
            f"{result.stderr.strip()}"
        )

    if not output.exists():
        raise RuntimeError(
            "FFmpeg completed but output clip was not created."
        )

    return output


# ---------------------------------------------------------
# ASYNC WRAPPERS
# ---------------------------------------------------------

async def build_index_async(
    source_path,
    index_path,
    sample_every=2.0,
    progress=None,
):
    return await asyncio.to_thread(
        make_index,
        source_path,
        index_path,
        sample_every,
        progress,
    )


async def search_index_async(
    query_path,
    index_data,
    sample_every=1.0,
):
    return await asyncio.to_thread(
        find_visual_candidates,
        query_path,
        index_data,
        sample_every,
    )


async def create_clip_async(
    source,
    start,
    end,
    output,
):
    return await asyncio.to_thread(
        create_clip,
        source,
        start,
        end,
        output,
    )
