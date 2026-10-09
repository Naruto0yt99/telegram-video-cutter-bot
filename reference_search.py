"""Free, local visual search over Telegram episode sources.

Builds a one-time coarse dHash index (one frame every two seconds), then searches
reference-video frames against that index. No paid AI API and no full source
episode downloads are required.
"""
import asyncio
from contextlib import contextmanager
import logging
import math
import re
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np
from telegram import Update
from telegram.ext import ContextTypes

from config import DATA_DIR, FFMPEG_BIN, TEMP_DIR
from database import get_connection
from ffmpeg_utils import run_command
from telegram_remote import open_telegram_range_server

logger = logging.getLogger("reference-search")
INDEX_PATH = DATA_DIR / "reference_visual_index.sqlite3"
INDEX_INTERVAL = 2.0
FRAME_WIDTH = 9
FRAME_HEIGHT = 8
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
MAX_HASH_DISTANCE = 13
MAX_MATCHES_PER_FRAME = 20
MAX_RESULT_CLIPS = 12


def _connect():
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY,
            source_url TEXT NOT NULL UNIQUE,
            anime TEXT NOT NULL,
            season TEXT NOT NULL,
            episode TEXT NOT NULL,
            quality TEXT NOT NULL,
            duration REAL NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS frames (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            timestamp REAL NOT NULL,
            hash BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reference_frames_source_time
            ON frames(source_id, timestamp);
    """)
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _dhash(raw_frame: bytes) -> bytes:
    """Return an 8-byte difference hash from a 9x8 grayscale frame."""
    pixels = np.frombuffer(raw_frame, dtype=np.uint8).reshape(FRAME_HEIGHT, FRAME_WIDTH)
    bits = pixels[:, 1:] > pixels[:, :-1]
    return np.packbits(bits.reshape(-1)).tobytes()


async def _read_frame_hashes(input_url: str, *, remote: bool, interval: float):
    """Yield (timestamp, 8-byte hash) without writing the source video to disk."""
    vf = f"fps=1/{float(interval):.6f},scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray"
    args = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-threads", "1"]
    if remote:
        args += [
            "-seekable", "1", "-multiple_requests", "1",
            "-initial_request_size", str(2 * 1024 * 1024),
            "-request_size", str(2 * 1024 * 1024),
            "-short_seek_size", str(2 * 1024 * 1024),
        ]
    args += [
        "-i", input_url, "-vf", vf, "-vsync", "0",
        "-f", "rawvideo", "-pix_fmt", "gray", "-"
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    index = 0
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readexactly(FRAME_BYTES), timeout=120
                )
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    logger.debug("Ignoring incomplete final frame (%s bytes)", len(exc.partial))
                break
            yield round(index * interval, 3), _dhash(raw)
            index += 1
        stderr = await process.stderr.read()
        code = await process.wait()
        if code:
            detail = stderr.decode("utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"FFmpeg frame scan failed: {detail}")
    except BaseException:
        if process.returncode is None:
            process.kill()
        try:
            await process.wait()
        except Exception:
            pass
        raise


async def _index_source(client, source):
    source_url = source["source_url"]
    server = await open_telegram_range_server(client, source_url)
    rows = []
    try:
        duration = float(getattr(server, "duration", 0) or 0)
        async for timestamp, signature in _read_frame_hashes(
            server.url, remote=True, interval=INDEX_INTERVAL
        ):
            rows.append((timestamp, sqlite3.Binary(signature)))
        if not rows:
            raise RuntimeError("No frames decoded from source.")
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO sources(source_url, anime, season, episode, quality, duration) "
                "VALUES(?,?,?,?,?,?)",
                (
                    source_url, source["anime"], str(source["season"]),
                    str(source["episode"]), str(source["quality"]), duration
                ),
            )
            source_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO frames(source_id, timestamp, hash) VALUES(?,?,?)",
                [(source_id, t, h) for t, h in rows],
            )
            conn.commit()
        return len(rows)
    finally:
        await server.close()


async def reference_index_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot as legacy_bot

    message = update.effective_message
    if not legacy_bot.is_owner(update.effective_user.id):
        await message.reply_text("❌ Owner only. Indexing 500+ hours of video can be resource-intensive.")
        return
    if legacy_bot.telethon_client is None:
        await message.reply_text("❌ Telegram USER_SESSION connected nahi hai.")
        return

    args = [x for x in context.args if x.lower() != "--force"]
    force = "--force" in [x.lower() for x in context.args]
    only_match = None
    if args:
        query = " ".join(args).strip()
        match = re.match(r"^(.+?)\\s+s(?:eason)?\\s*(\\d+)\\s*e(?:p(?:isode)?)?\\s*(\\d+)$", query, re.IGNORECASE)
        if not match:
            await message.reply_text(
                "Usage: /findindex Death Note S1 E1\n"
                "Sirf ek episode test karne ke liye anime name + season + episode do.\n"
                "Bina arguments ke /findindex poori library index karega."
            )
            return
        only_match = (
            re.sub(r"\\s+", " ", match.group(1).strip()).casefold(),
            int(match.group(2)),
            int(match.group(3)),
        )
    status = await message.reply_text(
        "🧠 VISUAL INDEX STARTED\\n\\n"
        + (f"🎯 Single episode: {query}\\n" if only_match else "📚 Mode: full library\\n")
        + "Har source video se 2-second interval par lightweight visual hashes banenge.\\n"
        "Full source episode file permanently download nahi hogi; sirf visual hashes SQLite index mein save honge.\\n"
        + ("⚠️ Existing index rebuild hoga.\\n" if force else "Already indexed sources skip honge.\\n")
        + "⏳ Indexing ke dauran temporary cache/storage phir bhi use ho sakti hai."
    )
    try:
        if force:
            with _db() as conn:
                conn.execute("DELETE FROM frames")
                conn.execute("DELETE FROM sources")
                conn.commit()

        with get_connection() as library:
            rows = library.execute(
                "SELECT anime, season, episode, quality, source_url "
                "FROM library ORDER BY id"
            ).fetchall()
        sources = []
        seen = set()
        for row in rows:
            item = dict(row)
            url = str(item.get("source_url") or "").strip()
            if url and url not in seen:
                item["source_url"] = url
                sources.append(item)
                seen.add(url)

        with _db() as conn:
            indexed = {row[0] for row in conn.execute("SELECT source_url FROM sources")}
        if only_match:
            target_anime, target_season, target_episode = only_match
            sources = [
                item for item in sources
                if re.sub(r"\\s+", " ", str(item.get("anime") or "").strip()).casefold() == target_anime
                and str(item.get("season") or "").strip().isdigit()
                and str(item.get("episode") or "").strip().isdigit()
                and int(str(item["season"]).strip()) == target_season
                and int(str(item["episode"]).strip()) == target_episode
            ]
            if not sources:
                await status.edit_text(
                    f"❌ Library mein {query} ka source nahi mila. /library se exact anime/episode check karo."
                )
                return
        queue = [item for item in sources if item["source_url"] not in indexed]
        done = 0
        frames_total = 0
        failures = []
        for item in queue:
            try:
                frames_total += await _index_source(legacy_bot.telethon_client, item)
                done += 1
            except Exception as exc:
                logger.exception("Reference index failed for %s", item["source_url"])
                failures.append(f"{item['anime']} S{item['season']} E{item['episode']}: {str(exc)[:160]}")
            if done % 5 == 0 or (done + len(failures)) == len(queue):
                try:
                    await status.edit_text(
                        "🧠 VISUAL INDEX RUNNING\n\n"
                        f"📚 Indexed this run: {done}/{len(queue)}\n"
                        f"🧩 New frame hashes: {frames_total:,}\n"
                        f"⚠️ Failed sources: {len(failures)}\n"
                        f"📦 Total library source links: {len(sources)}"
                    )
                except Exception:
                    pass

        with _db() as conn:
            final_sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            final_frames = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        await status.edit_text(
            "✅ VISUAL INDEX FINISHED\n\n"
            f"📦 Indexed source links: {final_sources}/{len(sources)}\n"
            f"🧩 Stored frame hashes: {final_frames:,}\n"
            f"➕ Added this run: {done}\n"
            f"⚠️ Failed this run: {len(failures)}\n\n"
            + ("First failures:\n" + "\n".join(failures[:5]) if failures else "Ab sabse pehle /findref use kar sakte ho.")
        )
    except Exception as exc:
        logger.exception("Reference visual indexing failed")
        await status.edit_text(f"❌ VISUAL INDEX FAILED\n\n{str(exc)[:1500]}")


def _load_index_arrays():
    import numpy as np

    with _db() as conn:
        source_rows = conn.execute(
            "SELECT id, source_url, anime, season, episode, quality, duration FROM sources"
        ).fetchall()
        source_info = {
            int(row[0]): {
                "source_url": row[1], "anime": row[2], "season": row[3],
                "episode": row[4], "quality": row[5], "duration": float(row[6] or 0)
            }
            for row in source_rows
        }
        if not source_info:
            return source_info, np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float32), np.empty(0, dtype=np.uint64)

        ids_parts, times_parts, hash_parts = [], [], []
        cursor = conn.execute("SELECT source_id, timestamp, hash FROM frames ORDER BY id")
        while True:
            batch = cursor.fetchmany(50000)
            if not batch:
                break
            ids_parts.append(np.fromiter((int(r[0]) for r in batch), dtype=np.int32, count=len(batch)))
            times_parts.append(np.fromiter((float(r[1]) for r in batch), dtype=np.float32, count=len(batch)))
            hash_parts.append(np.frombuffer(b"".join(bytes(r[2]) for r in batch), dtype=">u8").astype(np.uint64))
        return source_info, np.concatenate(ids_parts), np.concatenate(times_parts), np.concatenate(hash_parts)


async def _sample_reference(path: Path):
    return [item async for item in _read_frame_hashes(str(path), remote=False, interval=1.0)]


def _find_clusters(samples, source_info, source_ids, source_times, source_hashes):
    if not len(source_hashes):
        return []
    votes = defaultdict(dict)
    for ref_time, raw_hash in samples:
        target = np.frombuffer(raw_hash, dtype=">u8")[0].astype(np.uint64)
        distances = np.bitwise_count(np.bitwise_xor(source_hashes, target))
        k = min(MAX_MATCHES_PER_FRAME * 5, len(distances))
        nearest = np.argpartition(distances, k - 1)[:k]
        nearest = nearest[distances[nearest] <= MAX_HASH_DISTANCE]
        best_by_cluster = {}
        for pos in nearest:
            source_id = int(source_ids[pos])
            source_time = float(source_times[pos])
            offset = source_time - float(ref_time)
            # One vote per reference timestamp per source/offset neighbourhood.
            cluster_key = (source_id, int(math.floor(offset / 4.0)))
            candidate = (float(distances[pos]), source_time, float(ref_time), offset)
            previous = best_by_cluster.get(cluster_key)
            if previous is None or candidate[0] < previous[0]:
                best_by_cluster[cluster_key] = candidate
        for key, candidate in best_by_cluster.items():
            votes[key][round(float(ref_time), 2)] = candidate

    results = []
    for (source_id, _offset_bin), by_ref_time in votes.items():
        if len(by_ref_time) < 2:
            continue
        hits = list(by_ref_time.values())
        mean_distance = sum(item[0] for item in hits) / len(hits)
        if mean_distance > MAX_HASH_DISTANCE:
            continue
        ref_times = [item[2] for item in hits]
        source_times_hit = [item[1] for item in hits]
        if max(ref_times) - min(ref_times) > 45:
            continue
        duration = float(source_info[source_id]["duration"] or 0)
        start = max(0.0, min(source_times_hit) - 1.0)
        end = min(duration if duration > 0 else max(source_times_hit) + 2.0, max(source_times_hit) + 2.0)
        if end <= start:
            continue
        results.append({
            **source_info[source_id],
            "source_id": source_id,
            "ref_start": min(ref_times),
            "ref_end": max(ref_times),
            "start": start,
            "end": end,
            "hits": len(hits),
            "mean_distance": mean_distance,
            "confidence": max(0, min(100, int(100 * (1 - mean_distance / 64) * min(1, len(hits) / 5)))),
        })
    results.sort(key=lambda x: (-x["hits"], x["mean_distance"], x["ref_start"]))
    chosen = []
    for item in results:
        # Avoid near-identical hits from duplicate qualities or neighbouring offset bins.
        if any(
            item["source_url"] == old["source_url"]
            and abs(item["ref_start"] - old["ref_start"]) < 4
            and abs(item["start"] - old["start"]) < 5
            for old in chosen
        ):
            continue
        chosen.append(item)
        if len(chosen) >= MAX_RESULT_CLIPS:
            break
    return sorted(chosen, key=lambda x: x["ref_start"])


async def find_reference_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot as legacy_bot

    message = update.effective_message
    if not legacy_bot.is_owner(update.effective_user.id):
        await message.reply_text("❌ Owner only.")
        return
    if legacy_bot.telethon_client is None:
        await message.reply_text("❌ Telegram USER_SESSION connected nahi hai.")
        return
    reply = message.reply_to_message
    if reply is None:
        await message.reply_text(
            "Usage: reference video ko Telegram par bhejo, phir us video ko reply karke /findref likho.\n"
            "Reference file 20 MB se chhoti honi chahiye."
        )
        return

    media = getattr(reply, "video", None)
    if media is None and getattr(reply, "document", None) is not None:
        doc = reply.document
        if (getattr(doc, "mime_type", "") or "").lower().startswith("video/"):
            media = doc
    if media is None:
        await message.reply_text("❌ Reply mein video ya video document hona chahiye.")
        return
    if int(getattr(media, "file_size", 0) or 0) > 20 * 1024 * 1024:
        await message.reply_text("❌ Reference video 20 MB se badi hai. Isko 360p/low bitrate mein compress karke bhejo.")
        return

    with _db() as conn:
        indexed_count = int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
        frame_count = int(conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0])
    if indexed_count == 0 or frame_count == 0:
        await message.reply_text("⚠️ Pehle /findindex chalao. Ye one-time source indexing karega.")
        return

    status = await message.reply_text(
        "🔎 REFERENCE SEARCH STARTED\n\n"
        f"📦 Indexed source links: {indexed_count}\n"
        f"🧩 Searchable frame hashes: {frame_count:,}\n"
        "🎞️ Reference frames compare ho rahe hain..."
    )
    job_dir = Path(TEMP_DIR) / str(update.effective_user.id) / "reference_search"
    job_dir.mkdir(parents=True, exist_ok=True)
    reference_path = job_dir / f"reference_{reply.message_id}.mp4"
    sent_paths = []
    try:
        telegram_file = await media.get_file()
        await telegram_file.download_to_drive(custom_path=str(reference_path))
        samples = await _sample_reference(reference_path)
        if len(samples) < 2:
            raise RuntimeError("Reference video se enough frames decode nahi hue.")
        source_info, source_ids, source_times, source_hashes = _load_index_arrays()
        results = await asyncio.to_thread(
            _find_clusters, samples, source_info, source_ids, source_times, source_hashes
        )
        if not results:
            await status.edit_text(
                "😕 Strong visual match nahi mila.\n\n"
                "Possible reasons: scene par heavy crop/zoom/effects hain, source index incomplete hai, "
                "ya source video library mein nahi hai."
            )
            return

        await status.edit_text(f"✅ {len(results)} possible scene matches mile. Original source se clips extract ho rahe hain...")
        for index, item in enumerate(results, 1):
            output = job_dir / f"match_{index:02d}.mp4"
            server = None
            try:
                server = await open_telegram_range_server(legacy_bot.telethon_client, item["source_url"])
                await run_command(
                    FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
                    "-ss", f"{item['start']:.3f}", "-i", server.url,
                    "-t", f"{max(0.5, item['end'] - item['start']):.3f}",
                    "-map", "0:v:0?", "-map", "0:a:0?",
                    "-c", "copy", "-avoid_negative_ts", "make_zero", str(output)
                )
                if not output.exists() or output.stat().st_size == 0:
                    continue
                caption = (
                    f"🎯 Match {index}/{len(results)} · ~{item['confidence']}%\n"
                    f"📚 {item['anime']} S{item['season']} E{item['episode']} ({item['quality']})\n"
                    f"⏱ Source: {item['start']:.1f}s–{item['end']:.1f}s\n"
                    f"🎞 Reference: {item['ref_start']:.1f}s–{item['ref_end']:.1f}s\n"
                    f"🧩 Frame matches: {item['hits']} · avg dHash distance: {item['mean_distance']:.1f}/64"
                )
                await legacy_bot.send_file(update, output, caption)
                sent_paths.append(output)
            except Exception:
                logger.exception("Could not extract reference match %s", index)
            finally:
                if server is not None:
                    await server.close()
                output.unlink(missing_ok=True)
        await status.edit_text(
            "🏁 REFERENCE SEARCH COMPLETE\n\n"
            f"🔎 Candidate clusters: {len(results)}\n"
            "Clips upar bhej diye hain. Confidence approximate hai; best results same footage ke liye hain."
        )
    except Exception as exc:
        logger.exception("Reference search failed")
        await status.edit_text(f"❌ REFERENCE SEARCH FAILED\n\n{str(exc)[:1500]}")
    finally:
        reference_path.unlink(missing_ok=True)