import asyncio, base64, io, json, re, zlib
import numpy as np
from config import FFMPEG_BIN


def _caption_key(message):
    text = str(getattr(message, "message", "") or "")
    if "RAW FINGERPRINT" not in text.upper():
        return None
    m = re.search(r"📚\s*(.+?)\s+S(\d+)\s+E(\d+)", text, re.I)
    if not m:
        m = re.search(r"(?:^|\n)\s*(.+?)\s+S(\d+)\s+E(\d+)\b", text, re.I)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2)), int(m.group(3))


async def load_saved_fingerprints(
    client,
    chat_id,
    topic_id,
    wanted_keys=None,
    anime=None,
    season=None,
    max_items=40,
):
    if client is None or topic_id is None:
        return {}
    wanted = set(wanted_keys or [])
    found = {}
    async for message in client.iter_messages(chat_id, reply_to=int(topic_id)):
        key = _caption_key(message)
        if not key or key in found:
            continue
        if wanted and key not in wanted:
            continue
        if anime and str(key[0]).casefold() != str(anime).casefold():
            continue
        if season is not None and key[1] != int(season):
            continue
        try:
            buf = io.BytesIO()
            await client.download_media(message, file=buf)
            data = json.loads(buf.getvalue().decode("utf-8"))
            if data.get("type") != "episode_visual_fingerprint":
                continue
            found[key] = data
            if len(found) >= max_items:
                break
        except Exception:
            continue
    return found


def _decode_fp(fp):
    raw = zlib.decompress(base64.b64decode(str(fp["hashes"]).encode("ascii")))
    shape = tuple(int(x) for x in fp.get("shape", []))
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(shape).astype(np.float32)
    times = np.asarray(fp.get("times", []), dtype=np.float32)
    if len(times) != len(arr):
        times = np.arange(len(arr), dtype=np.float32) * float(fp.get("sample_every", 0.1))
    return arr, times


def _target_hash(frame):
    arr = frame.astype(np.float32)
    mean = float(arr.mean())
    std = max(1.0, float(arr.std()))
    norm = np.clip(((arr - mean) / std) * 32.0 + 128.0, 0, 255).astype(np.uint8)
    return norm.reshape(9, 10, 16, 10).mean(axis=(1, 3)).astype(np.float32)


async def _extract_target(path, start, end, fps=2.0):
    duration = max(0.5, float(end) - float(start))
    args = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(path),
        "-t", f"{duration:.3f}",
        "-vf", f"fps={fps},scale=160:90:flags=bilinear,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    raw = await p.stdout.read()
    err = await p.stderr.read()
    rc = await p.wait()
    if rc != 0:
        raise RuntimeError(err.decode("utf-8", "ignore")[:500] or "target fingerprint ffmpeg failed")
    frame_bytes = 160 * 90
    frames = [
        raw[i:i + frame_bytes]
        for i in range(0, len(raw), frame_bytes)
        if len(raw[i:i + frame_bytes]) == frame_bytes
    ]
    if not frames:
        return np.empty((0, 9, 10), dtype=np.float32)
    return np.stack([
        _target_hash(np.frombuffer(x, dtype=np.uint8).reshape(90, 160))
        for x in frames
    ])


async def target_fingerprint(path, start, end):
    return await _extract_target(path, start, end, fps=2.0)


def _score_alignment(target, source, start_idx):
    n = len(target)
    if start_idx < 0 or start_idx + n > len(source):
        return None
    a = target.reshape(n, -1)
    b = source[start_idx:start_idx + n].reshape(n, -1)
    d = np.mean(np.abs(a - b), axis=1) / 255.0
    return float(np.median(d) * 0.65 + np.percentile(d, 25) * 0.15 + np.mean(d) * 0.20)


def match_fingerprint(target, fp, top_n=3):
    if target.size == 0:
        return []
    source, times = _decode_fp(fp)
    step = max(1, int(round(0.5 / max(0.05, float(fp.get("sample_every", 0.1))))))
    source2 = source[::step]
    times2 = times[::step]
    if len(source2) < len(target):
        return []

    flat_t = target.reshape(len(target), -1)
    flat_s = source2.reshape(len(source2), -1)
    probe_idx = sorted(set([0, len(target) // 2, len(target) - 1]))
    seed_scores = np.zeros(len(source2), dtype=np.float32)
    for j in probe_idx:
        seed_scores += np.mean(np.abs(flat_s - flat_t[j]), axis=1) / 255.0
    seeds = np.argsort(seed_scores)[:min(30, len(seed_scores))]

    results = []
    for seed in seeds:
        for offset in range(-3, 4):
            idx = int(seed) + offset
            score = _score_alignment(target, source2, idx)
            if score is not None:
                results.append((score, idx))
    results.sort()

    chosen = []
    for score, idx in results:
        center = float(times2[min(len(times2) - 1, idx + len(target) // 2)])
        if all(abs(center - item["center"]) >= max(4.0, len(target) * 0.8) for item in chosen):
            chosen.append({"center": center, "score": score})
        if len(chosen) >= top_n:
            break
    return chosen
