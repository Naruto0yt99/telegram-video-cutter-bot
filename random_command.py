import logging
import random
import re
from pathlib import Path

from database import get_connection
from find_engine import _extract_remote_clip
from telegram_remote import get_telegram_video_info
from telethon_runtime import ensure_telethon_client

logger = logging.getLogger("anime-bot.random")

RANDOM_CLIP_SECONDS = 5 * 60
MAX_CANDIDATES = 30


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_") or "anime"


def _output_path(user_id, anime, season, episode):
    from config import TEMP_DIR

    directory = Path(TEMP_DIR) / str(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"random_{_safe_name(anime)}_S{season}E{episode}.mp4"
    return directory / name


def _random_sources(limit=MAX_CANDIDATES):
    """Return random library sources, lightly preferring smaller qualities for a 5-min test."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT anime, season, episode, quality, source_url
            FROM library
            ORDER BY CASE quality
                WHEN '360p' THEN 0
                WHEN '480p' THEN 1
                WHEN '720p' THEN 2
                WHEN '1080p' THEN 3
                WHEN '1440p' THEN 4
                WHEN '2160p' THEN 5
                ELSE 6
            END, RANDOM()
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


async def random_command(update, context):
    if not update.message:
        return

    if context.application.bot_data.get("random_job_running"):
        await update.message.reply_text("⏳ Ek /random test already chal raha hai.")
        return

    context.application.bot_data["random_job_running"] = True
    output = None

    try:
        await update.message.reply_text(
            "🎲 RANDOM TEST\n\n"
            "Library se random episode choose kar raha hoon..."
        )

        client = await ensure_telethon_client()
        if client is None:
            raise RuntimeError("Telegram USER_SESSION connected nahi hai.")

        candidates = _random_sources()
        if not candidates:
            raise RuntimeError("Library me koi source nahi mila.")

        selected = None
        duration = None

        for candidate in candidates:
            try:
                chat, message_id = _parse_source(candidate["source_url"])
                _message, source_duration, _size = await get_telegram_video_info(
                    client, chat, message_id
                )
                if source_duration < RANDOM_CLIP_SECONDS:
                    logger.info(
                        "Skipping short random source %s S%s E%s duration=%.1fs",
                        candidate["anime"], candidate["season"], candidate["episode"], source_duration,
                    )
                    continue

                selected = candidate
                duration = source_duration
                break
            except Exception as exc:
                logger.warning(
                    "Random candidate %s/%s/%s/%s failed: %s",
                    candidate.get("anime"), candidate.get("season"),
                    candidate.get("episode"), candidate.get("quality"), exc,
                )
                continue

        if selected is None or duration is None:
            raise RuntimeError(
                f"{len(candidates)} random library sources try kiye, lekin 5-minute usable video nahi mila."
            )

        max_start = max(0.0, duration - RANDOM_CLIP_SECONDS)
        start = random.uniform(0.0, max_start)
        end = start + RANDOM_CLIP_SECONDS

        output = _output_path(
            update.effective_user.id,
            selected["anime"],
            selected["season"],
            selected["episode"],
        )

        status = await update.message.reply_text(
            "🎲 RANDOM TEST\n\n"
            f"Anime: {selected['anime']}\n"
            f"Season: {selected['season']} | Episode: {selected['episode']}\n"
            f"Quality: {selected['quality']}\n"
            f"Random point: {start:.1f}s\n"
            "Telegram se required range fetch karke 5-min clip bana raha hoon..."
        )

        await _extract_remote_clip(client, selected["source_url"], start, end, output)

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("Random clip output empty hai.")

        from config import TELEGRAM_MAX_BYTES
        if output.stat().st_size > TELEGRAM_MAX_BYTES:
            raise RuntimeError(
                f"5-min random clip {output.stat().st_size / 1024 / 1024:.1f} MB bana; "
                f"configured Telegram limit {TELEGRAM_MAX_BYTES / 1024 / 1024:.0f} MB hai."
            )

        with output.open("rb") as video:
            await update.message.reply_video(
                video=video,
                caption=(
                    "🎲 RANDOM TEST PASS ✅\n\n"
                    f"🎬 {selected['anime']} S{selected['season']} E{selected['episode']}\n"
                    f"🎞️ {selected['quality']}\n"
                    f"⏱️ {start:.1f}s → {end:.1f}s\n"
                    "📌 Gemini/anime identification use nahi hui — library se random source only."
                ),
                supports_streaming=True,
            )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as exc:
        logger.exception("/random failed")
        await update.message.reply_text(f"❌ RANDOM TEST FAILED\n\n{exc}")
    finally:
        context.application.bot_data["random_job_running"] = False
        if output is not None:
            try:
                output.unlink(missing_ok=True)
            except Exception:
                pass


def _parse_source(source_url):
    from telegram_media import parse_telegram_message_link

    return parse_telegram_message_link(source_url)
