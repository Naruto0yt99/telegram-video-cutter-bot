import asyncio
import json
import logging
import mimetypes
import re
import difflib
from pathlib import Path

import httpx

from config import GEMINI_API_KEY, TEMP_DIR, FFMPEG_BIN, FINGERPRINT_CHAT
from fingerprint_storage import get_fingerprint_topic_id
from fingerprint_matcher import load_saved_fingerprints, target_fingerprint, match_fingerprint
from visual_matcher import find_visual_match
from database import get_all_sources_for_episode, get_animes, get_seasons, get_episodes
from library_nav import canonical_anime
from telegram_remote import get_telegram_video_info, open_telegram_range_server
from ffmpeg_utils import run_command
from telegram_media import parse_telegram_message_link
from utils import safe_filename, unique_path

logger = logging.getLogger("find-pipeline-v3")

GEMINI_ROOT = "https://generativelanguage.googleapis.com"
# Requested model first; modern fallback keeps the pipeline usable if the legacy
# model is unavailable for the account.
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
GEMINI_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CHUNK_BYTES = 512 * 1024
QUALITY_LOW_TO_HIGH = ("240p", "360p", "480p", "720p", "1080p", "1440p", "2160p", "auto")


def _headers():
    return {"x-goog-api-key": GEMINI_API_KEY}


async def _gemini_upload_path(path: Path):
    size = path.stat().st_size
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=180, write=180)) as client:
        r = await client.post(
            f"{GEMINI_ROOT}/upload/v1beta/files",
            headers={
                **_headers(),
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": path.name}},
        )
        r.raise_for_status()
        upload_url = r.headers.get("x-goog-upload-url")
        if not upload_url:
            raise RuntimeError("Gemini upload URL nahi mila.")
        async def body():
            with path.open("rb") as f:
                while True:
                    chunk = f.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk
        r = await client.post(
            upload_url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Type": mime,
            },
            content=body(),
        )
        r.raise_for_status()
        return r.json().get("file", {})


async def _gemini_upload_telegram(client, source_url: str, display_name: str):
    """Create a tiny visual proxy from Telegram and upload only that proxy to Gemini."""
    import os
    import tempfile

    chat, message_id = parse_telegram_message_link(source_url)
    _, duration, _ = await get_telegram_video_info(client, chat, message_id)

    server = await open_telegram_range_server(client, source_url)
    proxy = None
    try:
        fd, proxy = tempfile.mkstemp(prefix="gemini_proxy_", suffix=".mp4")
        os.close(fd)

        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-seekable", "1", "-multiple_requests", "1",
            "-initial_request_size", str(2 * 1024 * 1024),
            "-request_size", str(2 * 1024 * 1024),
            "-short_seek_size", str(2 * 1024 * 1024),
            "-i", server.url,
            "-vf", "fps=2,scale=-2:240",
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "31",
            "-movflags", "+faststart",
            proxy,
        ]
        last_error = None
        for attempt in range(3):
            try:
                await run_command(*cmd)
                if Path(proxy).exists() and Path(proxy).stat().st_size > 0:
                    break
            except Exception as exc:
                last_error = exc
                try:
                    os.remove(proxy)
                except OSError:
                    pass
                if attempt >= 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        if not Path(proxy).exists() or Path(proxy).stat().st_size == 0:
            raise last_error or RuntimeError("Telegram visual proxy empty bana.")

        path = Path(proxy)
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError("Gemini visual proxy empty bana.")

        last_error = None
        for attempt in range(3):
            try:
                file_data = await _gemini_upload_path(path)
                if not file_data.get("name"):
                    raise RuntimeError("Gemini ne visual proxy ko file ke roop me accept nahi kiya.")
                return file_data, duration, path.stat().st_size
            except Exception as exc:
                last_error = exc
                if attempt >= 2:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise last_error or RuntimeError("Gemini proxy upload failed.")
    finally:
        await server.close()
        if proxy:
            try:
                os.remove(proxy)
            except OSError:
                pass


async def _gemini_upload_telegram_window(client, source_url: str, start: float, end: float, display_name: str):
    """Upload only a small candidate window to Gemini for final verification."""
    import os
    import tempfile

    server = await open_telegram_range_server(client, source_url)
    proxy = None
    try:
        fd, proxy = tempfile.mkstemp(prefix="gemini_window_", suffix=".mp4")
        os.close(fd)
        duration = max(1.0, float(end) - float(start))
        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-seekable", "1", "-multiple_requests", "1",
            "-initial_request_size", str(2 * 1024 * 1024),
            "-request_size", str(2 * 1024 * 1024),
            "-short_seek_size", str(2 * 1024 * 1024),
            "-ss", f"{max(0.0, float(start) - 2.0):.3f}",
            "-i", server.url,
            "-t", f"{min(duration + 4.0, 55.0):.3f}",
            "-vf", "fps=2,scale=-2:240",
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "31",
            "-movflags", "+faststart", proxy,
        ]
        await run_command(*cmd)
        if not Path(proxy).exists() or Path(proxy).stat().st_size <= 0:
            raise RuntimeError("Gemini candidate window empty bana.")
        file_data = await _gemini_upload_path(Path(proxy))
        if not file_data.get("name"):
            raise RuntimeError("Gemini candidate window upload failed.")
        return file_data, duration
    finally:
        await server.close()
        if proxy:
            try:
                os.remove(proxy)
            except OSError:
                pass


async def _wait_active(name: str):
    deadline = asyncio.get_running_loop().time() + 180
    async with httpx.AsyncClient(timeout=60) as client:
        while asyncio.get_running_loop().time() < deadline:
            r = await client.get(f"{GEMINI_ROOT}/v1beta/{name}", headers=_headers())
            r.raise_for_status()
            data = r.json()
            state = data.get("state") or data.get("file", {}).get("state")
            if state in (None, "ACTIVE"):
                return data
            if state == "FAILED":
                raise RuntimeError(f"Gemini video processing failed: {data}")
            await asyncio.sleep(2)
    raise TimeoutError("Gemini video processing timeout.")


def _json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


async def _resolve_file_uri(client, name):
    # Gemini File API returns a resource name AND a separate file.uri. The
    # generateContent file_data.file_uri must use the latter, not the REST
    # resource URL. Older code guessed the URL from name, which can make every
    # model reject the video and produce the misleading "no regions" fallback.
    r = await client.get(f"{GEMINI_ROOT}/v1beta/{name}", headers=_headers())
    r.raise_for_status()
    data = r.json()
    file_obj = data.get("file", data)
    uri = file_obj.get("uri")
    if not uri:
        raise RuntimeError(f"Gemini file URI missing for {name}")
    return uri

async def _generate(prompt, files):
    last = None
    timeout = httpx.Timeout(60, read=600, write=120)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            uris = [await _resolve_file_uri(client, name) for name in files]
        except Exception as exc:
            logger.warning("Gemini file URI resolution failed: %s", exc)
            raise

        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    *[
                        {
                            "file_data": {
                                "mime_type": "video/mp4",
                                "file_uri": uri,
                            },
                            "media_processing": "STATIC",
                            "media_resolution": {"level": "MEDIA_RESOLUTION_LOW"},
                        }
                        for uri in uris
                    ],
                    {"text": prompt},
                ],
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": False},
            },
        }

        for model in GEMINI_MODELS:
            for attempt in range(2):
                try:
                    r = await client.post(
                        f"{GEMINI_ROOT}/v1beta/models/{model}:generateContent",
                        headers={**_headers(), "Content-Type": "application/json"},
                        json=payload,
                    )
                    if r.status_code in GEMINI_RETRYABLE_STATUS and attempt < 1:
                        retry_after = r.headers.get("retry-after")
                        try:
                            delay = min(20.0, max(2.0, float(retry_after)))
                        except (TypeError, ValueError):
                            delay = 2.0 * (attempt + 1)
                        logger.warning("Gemini retryable %s from %s; retrying in %.1fs", r.status_code, model, delay)
                        await asyncio.sleep(delay)
                        continue
                    if r.status_code >= 400:
                        body = r.text[:4000]
                        logger.error("Gemini HTTP %s model=%s body=%s", r.status_code, model, body)
                        if r.status_code not in GEMINI_RETRYABLE_STATUS:
                            raise RuntimeError(f"Gemini HTTP {r.status_code} ({model}): {body}")
                    r.raise_for_status()
                    try:
                        data = r.json()
                    except Exception as exc:
                        logger.error("Gemini returned non-JSON HTTP body model=%s body=%s", model, r.text[:4000])
                        raise RuntimeError(f"Gemini response JSON parse failed ({model}): {r.text[:1000]}") from exc
                    text_parts = []
                    for candidate in data.get("candidates", []):
                        for part in candidate.get("content", {}).get("parts", []):
                            if part.get("text"):
                                text_parts.append(part["text"])
                    raw_text = "\n".join(text_parts).strip()
                    if not raw_text:
                        logger.warning("Gemini returned empty text model=%s response=%s", model, str(data)[:4000])
                        raise RuntimeError(f"Gemini returned empty text ({model})")
                    try:
                        return _json(raw_text)
                    except Exception as exc:
                        logger.error("Gemini returned invalid JSON model=%s text=%s", model, raw_text[:4000])
                        raise RuntimeError(f"Gemini returned invalid JSON ({model}): {raw_text[:1000]}") from exc
                except Exception as exc:
                    last = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in GEMINI_RETRYABLE_STATUS and attempt < 2:
                        await asyncio.sleep(2.0 * (attempt + 1))
                        continue
                    logger.warning("Gemini model failed: %s: %s", model, exc)
                    break
    raise last or RuntimeError("Gemini request failed.")


def _normalize_regions(data):
    """Accept the strict regions schema plus common Gemini JSON variants."""
    if not isinstance(data, dict):
        return []
    raw = data.get("regions") or data.get("scenes") or data.get("shots") or data.get("segments")
    if isinstance(raw, dict):
        raw = raw.get("regions") or raw.get("scenes") or raw.get("shots") or raw.get("segments")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_time", item.get("start"))
        end = item.get("end_time", item.get("end"))
        if start is None or end is None:
            continue
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        normalized = dict(item)
        normalized["start_time"] = start
        normalized["end_time"] = end
        normalized["anime"] = str(item.get("anime") or item.get("title") or item.get("anime_title") or "").strip()
        out.append(normalized)
    return out


def _qualities(sources):
    return [q for q in QUALITY_LOW_TO_HIGH if q in sources]


def _source_pair(anime, season, episode):
    sources = get_all_sources_for_episode(anime, season, episode)
    if not sources:
        return None, None, {}
    qs = _qualities(sources)
    if not qs:
        return next(iter(sources.values())), next(iter(sources.values())), sources
    low_q = next((q for q in qs if q in ("240p", "360p")), qs[0])
    high_q = next((q for q in reversed(qs) if q != "auto"), qs[-1])
    return sources[low_q], sources[high_q], sources


def _library_anime_match(value):
    """Resolve Gemini's title against the actual DB without brittle exact matching.

    Gemini may return franchise subtitles, punctuation variants, romanization,
    or extra words such as "TV Series". The library stores the canonical DB
    title, so harmless naming differences must never kill FIND.
    """
    raw = str(value or "").strip()
    if not raw:
        return None

    canonical = canonical_anime(raw)
    try:
        db_names = [str(x).strip() for x in get_animes() if str(x).strip()]
    except Exception:
        db_names = []

    if not db_names:
        return canonical

    # Keep the real DB spelling as the value used by database.py.
    candidates = []
    for db_name in db_names:
        canon = canonical_anime(db_name) or db_name
        candidates.append((db_name, canon))
    candidates = list(dict.fromkeys(candidates))

    def norm(x):
        return re.sub(r"[^a-z0-9]+", "", str(x).casefold())

    def tokens(x):
        return set(re.findall(r"[a-z0-9]+", str(x).casefold()))

    raw_norm = norm(raw)
    raw_tokens = tokens(raw)
    canon_norm = norm(canonical) if canonical else ""

    # 1) Canonical alias match.
    if canon_norm:
        for db_name, db_canon in candidates:
            if norm(db_canon) == canon_norm:
                return db_name

    # 2) Exact normalized title.
    for db_name, _ in candidates:
        if norm(db_name) == raw_norm:
            return db_name

    # 3) Token/substring matching. This handles titles such as:
    # "Re:ZERO -Starting Life in Another World-" -> "Re:Zero"
    # "Death Note TV series" -> "Death Note"
    scored = []
    for db_name, db_canon in candidates:
        db_norm = norm(db_name)
        db_tokens = tokens(db_name)
        score = difflib.SequenceMatcher(None, raw_norm, db_norm).ratio()

        common = len(raw_tokens & db_tokens)
        if common:
            score += min(0.35, 0.12 * common)

        if raw_norm and (raw_norm in db_norm or db_norm in raw_norm):
            score += 0.35

        if canon_norm and (canon_norm in db_norm or db_norm in canon_norm):
            score += 0.25

        # Strong protection against accidental matches on a single tiny word.
        if raw_tokens and db_tokens and common >= min(2, len(raw_tokens)):
            score += 0.20

        scored.append((score, db_name))

    scored.sort(reverse=True, key=lambda x: x[0])
    if scored:
        best_score, best_name = scored[0]
        # A short title needs less similarity when it is a clear substring;
        # otherwise require meaningful overlap.
        if best_score >= 0.62 or (raw_norm and norm(best_name) in raw_norm):
            logger.info("Library anime resolver: %r -> %r (score %.3f)", raw, best_name, best_score)
            return best_name

    # Do not fabricate an anime name. Returning canonical is still useful when
    # the DB is empty or another resolver adds the title later.
    return canonical


def _episode_candidates(anime, season, episode):
    try:
        seasons = [str(x) for x in get_seasons(anime)]
    except Exception:
        seasons = []
    if not seasons:
        return [(anime, season, episode)]
    requested_season = str(season) if season is not None else None
    selected_season = requested_season if requested_season in seasons else None
    if selected_season is None and requested_season:
        m = re.search(r"\d+", requested_season)
        if m:
            wanted = int(m.group())
            numeric_seasons = []
            for x in seasons:
                mx = re.search(r"\d+", x)
                numeric_seasons.append((abs(int(mx.group()) - wanted), x) if mx else (10**9, x))
            selected_season = min(numeric_seasons)[1]
    if selected_season is None:
        selected_season = seasons[0]
    try:
        episodes = [str(x) for x in get_episodes(anime, selected_season)]
    except Exception:
        episodes = []
    if not episodes:
        return [(anime, selected_season, episode)]
    if episode is None:
        # No episode from Gemini is not an error. Give the fingerprint stage a
        # broad but bounded candidate set instead of arbitrarily taking E1-E8.
        ordered = sorted(episodes, key=lambda x: int(x) if x.isdigit() else 10**9)
        return [(anime, selected_season, e) for e in ordered[:80]]
    wanted = str(episode)
    nums = sorted(episodes, key=lambda x: int(x) if x.isdigit() else 10**9)
    if wanted in nums:
        pos = nums.index(wanted)
        ordered = nums[max(0, pos - 2):pos + 3]
    else:
        try:
            w = int(wanted)
            ordered = sorted(nums, key=lambda x: abs(int(x) - w))[:5]
        except Exception:
            ordered = nums[:5]
    return [(anime, selected_season, e) for e in dict.fromkeys(ordered)]


async def _extract_high_res(client, source_url, start, end, output):
    server = await open_telegram_range_server(client, source_url)
    try:
        await run_command(
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-ss", f"{float(start):.3f}",
            "-i", server.url,
            "-t", f"{max(0.05, float(end) - float(start)):.3f}",
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            str(output),
        )
    finally:
        await server.close()
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("High-quality stream-copy clip empty bana.")
    return output


async def run_find_v3(input_video: Path, user_id: int, telegram_client, progress_message=None):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing.")
    if telegram_client is None:
        raise RuntimeError("Telegram USER_SESSION connected nahi hai.")

    job_dir = Path(TEMP_DIR) / str(user_id) / "find_v3"
    job_dir.mkdir(parents=True, exist_ok=True)

    async def progress(text):
        if progress_message:
            try:
                await progress_message.edit_text(text)
            except Exception:
                pass

    await progress("🎯 FIND — 10%\n\n🧠 YouTube Short ko Gemini analyse kar raha hai...")
    edit_file = await _gemini_upload_path(Path(input_video))
    await _wait_active(edit_file["name"])

    # First pass identifies the source anime/episode candidates and the visual
    # evidence that will be used for the direct episode comparison.
    analysis = await _generate(
        """Watch VIDEO 1, the YouTube Short carefully. This is an evidence-first
scene identification pass. Return EVERY contiguous anime shot, even when the
exact season or episode is uncertain.

For each shot return:
- start_time and end_time in the Short
- anime title (best identification; never use generic labels)
- season and episode ONLY when supported by visible evidence
- distinctive chronological visual description
- characters, location/background, important actions and visual landmarks
- source_start_hint only when you are genuinely confident

Do NOT return an empty regions list just because episode/timestamp is uncertain.
Do NOT invent an episode number. The next stage will verify the scene against the
actual Telegram episode.

Return JSON only:
{"regions":[{"start_time":0,"end_time":5,"anime":"Naruto","season":1,"episode":27,
"source_start_hint":null,"description":"specific chronological action, characters,
background and camera movement","characters":["..."],"location":"...","landmarks":["..."]}]}""",
        [edit_file["name"]],
    )
    regions = _normalize_regions(analysis)
    if not regions:
        logger.warning("Gemini first-pass returned no normalized regions: %s", str(analysis)[:2000])
        retry_analysis = await _generate(
            """Re-analyze VIDEO 1. Identify the anime footage in the Short by VISUAL CONTENT only.
Return every distinct contiguous shot as a list. Do not require certainty about season, episode,
or timestamp. If the exact anime is uncertain, give your best title guess and leave season/episode
null. Never return an empty list when visible video frames contain anime footage.

Return JSON only:
{"regions":[{"start_time":0,"end_time":3,"anime":"best title guess","season":null,"episode":null,
"description":"specific visible characters, setting, action, colors and camera movement",
"characters":[],"location":"","landmarks":[]}]}

The important requirement is to describe what is visibly present, not to explain uncertainty.""",
            [edit_file["name"]],
        )
        regions = _normalize_regions(retry_analysis)
    if not regions:
        raise RuntimeError("Gemini ko Short me koi usable scene nahi mila.")

    await progress(f"🎯 FIND — 25%\n\n📚 {len(regions)} scenes identify ho gaye.\n🔎 Telegram library se exact source episode locate ho raha hai...")

    # Group by episode so an episode is streamed to Gemini only once even if
    # several Short scenes came from it.
    groups = {}
    for r in regions:
        anime = _library_anime_match(r.get("anime"))
        if not anime:
            continue
        season = r.get("season")
        episode = r.get("episode")
        try:
            season = int(season) if season is not None else None
        except (TypeError, ValueError):
            season = None
        try:
            episode = int(episode) if episode is not None else None
        except (TypeError, ValueError):
            episode = None
        for cand_anime, cand_season, cand_episode in _episode_candidates(anime, season, episode):
            try:
                cand_season_i = int(cand_season)
                cand_episode_i = int(cand_episode)
            except (TypeError, ValueError):
                continue
            low, high, sources = _source_pair(cand_anime, cand_season_i, cand_episode_i)
            if low and high:
                groups.setdefault((cand_anime, cand_season_i, cand_episode_i), (low, high, sources))
    if not groups:
        # Resolver/episode uncertainty is recoverable. Only report a hard
        # failure after all normalized library candidates were exhausted.
        seen = sorted({str(r.get("anime") or "").strip() for r in regions if r.get("anime")})
        raise RuntimeError(
            "FIND source candidates nahi mile. Gemini titles=%s; "
            "library resolver ne available Telegram sources me koi usable episode nahi paya."
            % (", ".join(seen[:8]) or "unknown")
        )

    # Fingerprint-first retrieval:
    #   Short -> saved episode fingerprints -> timestamp candidates
    #        -> bounded visual matcher -> tiny Gemini verification window.
    # This completely removes the old "upload every full episode to Gemini" path.
    fingerprint_topic = get_fingerprint_topic_id()
    fingerprint_cache = {}
    visual_candidates = []

    unique_anime_seasons = sorted({
        (key[0], key[1])
        for key in groups
    }, key=lambda x: (str(x[0]).casefold(), int(x[1]))
    )

    for anime_name, season_no in unique_anime_seasons:
        if fingerprint_topic is None:
            break
        try:
            fingerprint_cache[(anime_name, season_no)] = await load_saved_fingerprints(
                telegram_client,
                FINGERPRINT_CHAT,
                fingerprint_topic,
                anime=anime_name,
                season=season_no,
                max_items=80,
            )
        except Exception as exc:
            logger.warning("Fingerprint load failed for %s S%s: %s", anime_name, season_no, exc)
            fingerprint_cache[(anime_name, season_no)] = {}

    await progress(
        "🎯 FIND — 40%\\n\\n"
        "🧠 Saved fingerprints se timestamp candidates nikale ja rahe hain...\\n"
        "⚡ Full episode Gemini upload skip."
    )

    for idx, region in enumerate(regions, 1):
        anime = _library_anime_match(region.get("anime"))
        try:
            season = int(region.get("season")) if region.get("season") is not None else None
        except (TypeError, ValueError):
            season = None
        try:
            episode = int(region.get("episode")) if region.get("episode") is not None else None
        except (TypeError, ValueError):
            episode = None
        if not anime:
            continue

        possible_keys = [
            key for key in groups
            if key[0].casefold() == str(anime).casefold()
            and (season is None or key[1] == season)
            and (episode is None or key[2] == episode or abs(key[2] - episode) <= 2)
        ]
        if not possible_keys:
            possible_keys = [
                key for key in groups
                if key[0].casefold() == str(anime).casefold()
                and (season is None or key[1] == season)
            ]

        target = None
        fingerprint_hits = []
        for key in possible_keys:
            fp = fingerprint_cache.get((key[0], key[1]), {}).get(key)
            if not fp:
                continue
            if target is None:
                try:
                    target = await target_fingerprint(
                        Path(input_video),
                        float(region.get("start_time", 0) or 0),
                        float(region.get("end_time", 0) or 0),
                    )
                except Exception as exc:
                    logger.warning("Target fingerprint failed for scene %s: %s", idx, exc)
                    break
            for hit in match_fingerprint(target, fp, top_n=2):
                fingerprint_hits.append({
                    "key": key,
                    "fp": fp,
                    "center": float(hit["center"]),
                    "fp_score": float(hit["score"]),
                    "source_url": groups[key][0],
                    "high": groups[key][1],
                    "duration": float(fp.get("duration", 0) or 0),
                })

        fingerprint_hits.sort(key=lambda x: x["fp_score"])
        fingerprint_hits = fingerprint_hits[:3]

        if not fingerprint_hits:
            logger.info("Scene %s: no saved fingerprint candidate; skipping rather than scanning full episodes.", idx)
            continue

        for hit in fingerprint_hits[:2]:
            key = hit["key"]
            candidate_region = dict(region)
            candidate_region["source_start_hint"] = hit["center"]
            try:
                visual = await find_visual_match(
                    telegram_client,
                    {"source_url": hit["source_url"]},
                    candidate_region,
                    Path(input_video),
                    job_dir / f"visual_{idx}_{key[2]}",
                    hit["duration"],
                )
            except Exception as exc:
                logger.warning(
                    "Scene %s visual refine failed for %s S%s E%s: %s",
                    idx, key[0], key[1], key[2], exc
                )
                continue
            if not visual:
                continue
            visual_candidates.append({
                "index": idx,
                "region": region,
                "key": key,
                "source_url": hit["source_url"],
                "high": hit["high"],
                "duration": hit["duration"],
                "fingerprint_score": hit["fp_score"],
                "visual": visual,
            })

        await progress(
            f"🎯 FIND — {40 + int(30 * idx / max(1, len(regions)))}%\\n\\n"
            f"🔎 Scene {idx}/{len(regions)}\\n"
            "⚡ Fingerprint → visual refinement complete."
        )

    await progress(
        "🎯 FIND — 72%\\n\\n"
        "🧠 Sirf tiny candidate windows Gemini se final verify ho rahe hain..."
    )

    results = []
    best_by_scene = {}
    for candidate in sorted(
        visual_candidates,
        key=lambda x: (x["index"], x["fingerprint_score"], x["visual"].get("score", 99.0)),
    ):
        idx = candidate["index"]
        if idx in best_by_scene:
            continue

        region = candidate["region"]
        key = candidate["key"]
        visual = candidate["visual"]
        start = float(visual["start"])
        end = float(visual["end"])
        window_file = None
        try:
            window_file, _ = await _gemini_upload_telegram_window(
                telegram_client,
                candidate["source_url"],
                start,
                end,
                f"{safe_filename(key[0])}_S{key[1]:02d}E{key[2]:03d}_candidate.mp4",
            )
            await _wait_active(window_file["name"])

            prompt = f"""VIDEO 1 is one contiguous edited anime shot from a YouTube Short.
VIDEO 2 is a SMALL candidate window extracted from the user's original Telegram episode.
Verify whether VIDEO 2 contains the exact same visual action/continuity as VIDEO 1.

Known candidate: {key[0]} S{key[1]} E{key[2]}.
Short timing: {float(region.get("start_time", 0)):.3f}-{float(region.get("end_time", 0)):.3f}.
Fingerprint candidate center: {candidate["fingerprint_score"]:.4f}.
Visual matcher score: {float(visual.get("score", 99.0)):.4f}.
Description: {region.get("description", "")}

Return JSON only:
{{"match":true,"start":0.0,"end":0.0,"confidence":0.0,"speed":1.0}}
start/end MUST be ORIGINAL EPISODE seconds.
The candidate window is already near the match; refine within it.
Account for intro/outro offsets, speed changes, crops, subtitles and transitions.
Do not accept a merely similar character or background. Confidence below 0.80 means match=false."""
            match = await _generate(prompt, [edit_file["name"], window_file["name"]])
            if not isinstance(match, dict) or not match.get("match"):
                continue
            exact_start = float(match.get("start", start) or start)
            exact_end = float(match.get("end", end) or end)
            if exact_end <= exact_start:
                continue
            best_by_scene[idx] = True
            results.append({
                "index": idx,
                "anime": key[0],
                "season": key[1],
                "episode": key[2],
                "start": exact_start,
                "end": exact_end,
                "edit_start": float(region.get("start_time", 0) or 0),
                "edit_end": float(region.get("end_time", 0) or 0),
                "speed": float(match.get("speed", 1.0) or 1.0),
                "confidence": float(match.get("confidence", 0) or 0),
                "high": candidate["high"],
            })
        except Exception as exc:
            logger.warning("Scene %s final candidate verification failed: %s", idx, exc)
        finally:
            # Gemini candidate files are temporary; no local file is retained.
            # Avoid an extra API round-trip here because the File API lifecycle
            # already handles temporary uploads.
            pass

    if not results:
        raise RuntimeError("Gemini ne Telegram episode me koi reliable exact interval confirm nahi kiya.")

    results.sort(key=lambda x: x["edit_start"])
    clips = []
    await progress(f"🎯 FIND — 80%\n\n✂️ {len(results)} exact intervals mil gaye.\n⚡ Highest-quality Telegram stream se stream-copy cutting...")

    for i, item in enumerate(results, 1):
        output = job_dir / f"clip_{i:03d}.mp4"
        await _extract_high_res(
            telegram_client, item["high"], item["start"], item["end"], output
        )
        item["path"] = output
        clips.append(output)

    merged = job_dir / "final.mp4"
    concat = job_dir / "concat.txt"
    concat.write_text(
        "\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p in clips),
        encoding="utf-8",
    )
    try:
        await run_command(
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c", "copy", "-movflags", "+faststart", str(merged),
        )
    finally:
        concat.unlink(missing_ok=True)

    await progress("🎯 FIND — 95%\n\n📦 Final high-quality clip ready.\n📤 Telegram par upload ho raha hai...")
    report = "\n".join(
        f"{x['index']:02d} | {x['anime']} S{x['season']} E{x['episode']} | "
        f"RAW {x['start']:.3f}s → {x['end']:.3f}s | confidence {x['confidence']:.0%}"
        for x in results
    )
    return {
        "output": merged,
        "clips": [
            {
                "path": x["path"],
                "index": x["index"],
                "anime": x["anime"],
                "season": x["season"],
                "episode": x["episode"],
                "start": x["start"],
                "end": x["end"],
                "edit_start": x["edit_start"],
                "edit_end": x["edit_end"],
                "edit_duration": max(0.0, x["edit_end"] - x["edit_start"]),
                "speed": x["speed"],
                "confidence": x["confidence"],
            }
            for x in results
        ],
        "total": len(regions),
        "qa": {"match": True, "confidence": min(x["confidence"] for x in results)},
        "report": "📋 EXACT SOURCE TIMESTAMPS\n\n" + report,
        "sources": {(x["anime"].lower(), x["season"], x["episode"]) for x in results},
    }
