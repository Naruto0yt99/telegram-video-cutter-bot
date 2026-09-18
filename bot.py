import asyncio
import json
import fcntl
import logging
import re
import shutil
from html import escape
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import MemorySession
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import (
    BOT_TOKEN,
    OWNER_ID,
    TG_API_ID,
    TG_API_HASH,
    TELEGRAM_SESSION,
    TEMP_DIR,
    TELEGRAM_MAX_BYTES,
    FFMPEG_BIN,
    validate_bot_config,
)

from database import (
    init_db,
    add_source,
    get_animes,
    get_seasons,
    get_episodes,
    get_all_sources_for_episode,
    get_best_source,
    delete_source,
    delete_episode,
    delete_season,
    get_connection,
)

from telegram_media import (
    parse_telegram_message_link,
    download_telethon_message,
    download_bot_video,
)

from ffmpeg_utils import (
    get_duration,
    make_clip,
    split_video,
    parse_time,
    format_time,
    run_command,
)

from find_engine import find_and_build, _extract_remote_clip
from yt_downloader import download_video_from_url
from source_sync import sync_source_library, render_library_html
from utils import unique_path
from clip_handler import clip_command as source_clip_command
from library_nav import library_command as nav_library_command, library_callback as nav_library_callback


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("anime-bot")

telethon_client = None
bot_mtproto_client = None
source_sync_task = None
job_lock = asyncio.Lock()
active_videos = {}
_instance_lock_handle = None


def user_temp_dir(user_id: int) -> Path:
    path = Path(TEMP_DIR) / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_user_temp(user_id: int):
    path = user_temp_dir(user_id)
    try:
        if path.exists():
            shutil.rmtree(path)
    except Exception:
        logger.exception("Temp cleanup failed")

    active_videos.pop(user_id, None)


def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and int(user_id) == int(OWNER_ID)


async def send_file(update: Update, path: Path, caption: str):
    size = path.stat().st_size
    if size > TELEGRAM_MAX_BYTES:
        if bot_mtproto_client is None:
            raise RuntimeError("Large Telegram file ke liye MTProto sender connected nahi hai.")
        await bot_mtproto_client.send_file(
            update.effective_chat.id,
            str(path),
            caption=caption,
            supports_streaming=True,
        )
        return

    with path.open("rb") as f:
        await update.message.reply_video(video=f, caption=caption, supports_streaming=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 ANIME VIDEO BOT\n\n"
        "📚 /library — clickable source library\n"
        "🎯 /find <YouTube URL> — simple Gemini timestamp finder\n"
        "✂️ /clip 01:20 - 01:50 — active video se clip\n"
        "✂️ /clips Anime S1 E1 01:20 - 01:50 — source episode se clip\n"
        "✂️ /split 30 — active/original video ko parts me baanto\n"
        "♻️ /next — current original video reset\n"
        "💾 /save — owner-only manual source save\n"
        "✏️ /edit — owner-only source management\n"
        "❓ /help — detailed examples"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 HELP\n\n"
        "🎯 FIND\n"
        "/find https://youtube.com/shorts/xxxxx\n"
        "→ YouTube edit download\n"
        "→ Gemini edit ko dekhega\n"
        "→ Gemini anime + season + episode + approximate source time dega\n"
        "→ bot usi timestamp par source episode se clip nikalega\n"
        "→ koi visual matching / fingerprint / verification nahi\n\n"
        "✂️ CLIP (active/original video)\n"
        "/clip 01:20 - 01:50\n\n"
        "📺 SOURCE EPISODE CLIP\n"
        "/clips Naruto S3 E4 12:00 - 13:35\n"
        "→ Telegram source episode se direct clip\n\n"
        "✂️ SPLIT\n"
        "/split 30\n"
        "/split 60\n\n"
        "♻️ NEXT\n"
        "/next\n\n"
        "📚 LIBRARY\n"
        "/library\n\n"
        "💾 SAVE / EDIT owner-only commands bhi supported hain."
    )


async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        animes = get_animes()
        logger.info("/library requested: anime_count=%s", len(animes))

        if not animes:
            with get_connection() as conn:
                row = conn.execute("SELECT COUNT(*) AS count FROM library").fetchone()
                total = int(row["count"])
            logger.warning("/library empty: library row count=%s", total)
            await update.message.reply_text(
                "📚 Library abhi empty hai.\n"
                "AnimeNation012 ka automatic source scan background me chal raha ho sakta hai."
            )
            return

        text = render_library_html(
            animes,
            get_seasons,
            get_episodes,
            get_all_sources_for_episode,
        )

        max_chars = 3800
        chunks = []
        current = []
        current_len = 0

        for line in text.splitlines():
            addition = len(line) + (1 if current else 0)
            if current and current_len + addition > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += addition

        if current:
            chunks.append("\n".join(current))

        for index, chunk in enumerate(chunks, start=1):
            if index == 1:
                await update.message.reply_text(
                    chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    f"📚 <b>LIBRARY — {index}/{len(chunks)}</b>\n\n{chunk}",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

    except Exception as exc:
        logger.exception("/library failed")
        await update.message.reply_text(f"❌ Library load failed: {exc}")


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Owner only.")
        return

    anime = " ".join(context.args).strip()
    if not anime:
        await update.message.reply_text("Usage:\n/save Naruto")
        return

    context.user_data.clear()
    context.user_data["save_anime"] = anime
    context.user_data["save_step"] = "season_count"
    await update.message.reply_text(f"💾 Saving: {anime}\n\nKitne seasons hain?\nExample: 3")


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Owner only.")
        return

    await update.message.reply_text(
        "✏️ EDIT\n\n"
        "/edit add Anime S1 E1 1080p URL\n"
        "/edit delete Anime S1 E1 1080p\n"
        "/edit delete_episode Anime S1 E1\n"
        "/edit delete_season Anime S1\n"
        "/edit delete_anime Anime"
    )


async def edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Owner only.")
        return

    raw = " ".join(context.args).strip()
    if not raw:
        await edit_command(update, context)
        return

    try:
        # Keep anime names with spaces intact.
        add_match = re.match(
            r"^add\s+(.+?)\s+[Ss](\d+)\s+[Ee](\d+)\s+(\S+)\s+(https?://\S+)$",
            raw,
            re.IGNORECASE,
        )
        delete_match = re.match(
            r"^delete\s+(.+?)\s+[Ss](\d+)\s+[Ee](\d+)\s+(\S+)$",
            raw,
            re.IGNORECASE,
        )
        episode_match = re.match(
            r"^delete_episode\s+(.+?)\s+[Ss](\d+)\s+[Ee](\d+)$",
            raw,
            re.IGNORECASE,
        )
        season_match = re.match(
            r"^delete_season\s+(.+?)\s+[Ss](\d+)$",
            raw,
            re.IGNORECASE,
        )
        anime_delete_match = re.match(r"^delete_anime\s+(.+)$", raw, re.IGNORECASE)

        if add_match:
            anime, season, episode, quality, url = add_match.groups()
            parse_telegram_message_link(url)
            add_source(anime.strip(), season, episode, quality.lower(), url)
            await update.message.reply_text("✅ Source saved.")
        elif delete_match:
            anime, season, episode, quality = delete_match.groups()
            delete_source(anime.strip(), season, episode, quality.lower())
            await update.message.reply_text("✅ Source deleted.")
        elif episode_match:
            anime, season, episode = episode_match.groups()
            delete_episode(anime.strip(), season, episode)
            await update.message.reply_text("✅ Episode deleted.")
        elif season_match:
            anime, season = season_match.groups()
            delete_season(anime.strip(), season)
            await update.message.reply_text("✅ Season deleted.")
        elif anime_delete_match:
            anime = anime_delete_match.group(1).strip()
            with get_connection() as conn:
                conn.execute("DELETE FROM library WHERE LOWER(anime) = LOWER(?)", (anime,))
                conn.commit()
            await update.message.reply_text("✅ Anime deleted from library.")
        else:
            raise ValueError(
                "Format:\\n"
                "/edit add Anime Name S1 E1 1080p https://t.me/channel/123\\n"
                "/edit delete Anime Name S1 E1 1080p\\n"
                "/edit delete_episode Anime Name S1 E1\\n"
                "/edit delete_season Anime Name S1\\n"
                "/edit delete_anime Anime Name"
            )
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")

async def process_save_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("save_step")
    if not step:
        return False
    text = (update.message.text or "").strip()

    if step == "season_count":
        if not text.isdigit() or not 1 <= int(text) <= 50:
            await update.message.reply_text("❌ Seasons 1-50 ke beech number bhejo.")
            return True
        context.user_data["save_season_count"] = int(text)
        context.user_data["save_current_season"] = 1
        context.user_data["save_step"] = "episode_count"
        await update.message.reply_text("📺 Season 1 me kitne episodes hain?")
        return True

    if step == "episode_count":
        if not text.isdigit() or not 1 <= int(text) <= 5000:
            await update.message.reply_text("❌ Valid episode count bhejo.")
            return True
        season = context.user_data["save_current_season"]
        context.user_data["save_episode_count"] = int(text)
        context.user_data["save_step"] = "episode_links"
        await update.message.reply_text(
            f"🔗 Season {season}: {text} Telegram links bhejo, one per line.\n"
            "Pehli line = Episode 1, dusri = Episode 2..."
        )
        return True

    if step == "episode_links":
        links = [x.strip() for x in text.splitlines() if x.strip()]
        expected = context.user_data["save_episode_count"]
        if len(links) != expected:
            await update.message.reply_text(f"❌ {expected} links chahiye the; {len(links)} mile.")
            return True
        anime = context.user_data["save_anime"]
        season = context.user_data["save_current_season"]
        saved = 0
        for index, url in enumerate(links, start=1):
            try:
                parse_telegram_message_link(url)
                add_source(anime, season, index, "auto", url)
                saved += 1
            except Exception as exc:
                logger.warning("Invalid source %s: %s", url, exc)
        total_seasons = context.user_data["save_season_count"]
        if season < total_seasons:
            context.user_data["save_current_season"] = season + 1
            context.user_data["save_step"] = "episode_count"
            await update.message.reply_text(f"✅ Season {season}: {saved}/{expected} saved.\n📺 Season {season + 1} ka episode count bhejo.")
        else:
            anime_name = context.user_data["save_anime"]
            context.user_data.clear()
            await update.message.reply_text(f"🎉 SAVE COMPLETE\nAnime: {anime_name}\nLast season: {saved}/{expected}")
        return True

    return False


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage:\n/find https://youtube.com/shorts/xxxxx")
        return

    url = context.args[0]
    if not re.match(r"^https?://", url, re.I):
        await update.message.reply_text("❌ Valid URL bhejo.")
        return
    if job_lock.locked():
        await update.message.reply_text("⏳ Ek find job already chal raha hai.")
        return

    status = await update.message.reply_text("🎯 FIND STARTED\n\n1️⃣ Video download ho raha hai...")
    user_id = update.effective_user.id

    try:
        async with job_lock:
            video_path = await download_video_from_url(url, user_id)
            await status.edit_text("🎯 FIND\n\n1️⃣ Video downloaded ✅\n2️⃣ Gemini scene analysis...")
            result = await find_and_build(
                input_video=video_path,
                user_id=user_id,
                telethon_client=telethon_client,
                progress_message=status,
            )

            clips = result.get("clips", [])
            await status.edit_text(
                "🎯 FIND\n\n"
                f"Gemini scenes: {result.get('total', len(clips))}\n"
                f"Clips ready: {len(clips)}\n\n"
                "📤 Clips bheje ja rahe hain..."
            )

            for item in clips:
                caption = (
                    f"🎬 Video\n"
                    f"Clip {item['index']}\n"
                    f"{item['anime']} S{item['season']} E{item['episode']} "
                    f"{format_time(item['start'])} - {format_time(item['end'])}\n"
                    f"⚠️ Approximate timestamp — manually adjust with /clips if needed."
                )
                await send_file(update, Path(item["path"]), caption)

            if len(clips) < result.get("total", len(clips)):
                await status.edit_text(
                    "🎯 FIND COMPLETE\n\n"
                    f"{len(clips)}/{result.get('total')} clips ready.\n"
                    "Jin scenes ka source nahi mila, unhe skip kiya gaya."
                )
            else:
                await status.edit_text("🎯 FIND COMPLETE ✅\n\nSab approximate clips bhej diye.")
    except Exception as exc:
        logger.exception("Find failed")
        await status.edit_text(f"❌ FIND FAILED\n\n{exc}")
    finally:
        cleanup_user_temp(user_id)


async def _get_active_video(user_id: int):
    path_text = active_videos.get(user_id)
    if not path_text:
        return None
    path = Path(path_text)
    if not path.exists() or path.stat().st_size == 0:
        active_videos.pop(user_id, None)
        return None
    return path


async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    source_mode = False
    source_url = None

    try:
        if len(args) == 3 and args[1] == "-":
            input_path = await _get_active_video(user_id)
            if input_path is None:
                raise ValueError("Pehle original video bhejo.")
            start = parse_time(args[0])
            end = parse_time(args[2])
        elif len(args) == 6 and args[4] == "-":
            anime = args[0]
            season = args[1].lstrip("Ss")
            episode = args[2].lstrip("Ee")
            start = parse_time(args[3])
            end = parse_time(args[5])
            source_url = get_best_source(anime, season, episode)
            if not source_url:
                raise ValueError("Source episode library me nahi mila.")
            from telethon_runtime import ensure_telethon_client
            source_client = await ensure_telethon_client()
            source_mode = True
            input_path = None
        else:
            raise ValueError(
                "Usage:\n/clip 01:20 - 01:50\n"
                "or\n/clips Anime S1 E1 01:20 - 01:50"
            )

        if source_mode:
            output = unique_source_clip_path(user_id, anime, season, episode)
            await _extract_remote_clip(source_client, source_url, start, end, output)
            caption = f"✂️ {anime} S{season} E{episode} {format_time(start)} - {format_time(end)}"
            await send_file(update, output, caption)
            return

        duration = await get_duration(input_path)
        if start < 0 or end <= start or end > duration:
            raise ValueError(f"Video duration {format_time(duration)} hai.")
        output = await make_clip(input_path, start, end, name="clip")
        await send_file(update, output, f"✂️ {format_time(start)} → {format_time(end)}")

    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")


def unique_source_clip_path(user_id, anime, season, episode):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"clip_{anime}_S{season}E{episode}")
    return unique_path(user_temp_dir(user_id), name + ".mp4")


async def _stream_remote_split(update, status, source_client, source_url, output_dir, part_duration, total_parts, anime, season, episode):
    """Extract remote split parts concurrently, then send them in order.

    Each part gets its own FFmpeg seek against the Telegram range server.
    This avoids waiting for a single long segment-muxer pass before the first
    clip becomes available. Parts are generated concurrently, but Telegram
    messages are sent strictly in part order so they appear one below another.
    """
    from telegram_remote import open_telegram_range_server

    server = await open_telegram_range_server(source_client, source_url)
    semaphore = asyncio.Semaphore(min(3, max(1, total_parts)))
    generated = {}
    errors = {}

    async def build_part(index):
        start_time = index * part_duration
        duration = min(part_duration, max(0.0, server.duration - start_time))
        if duration <= 0:
            return

        output = output_dir / f"part_{index:03d}.mp4"
        async with semaphore:
            try:
                await run_remote_part(
                    server.url,
                    start_time,
                    duration,
                    output,
                )
                if not output.exists() or output.stat().st_size <= 0:
                    raise RuntimeError("Empty split output.")
                generated[index] = output
            except Exception as exc:
                errors[index] = str(exc)
                logger.exception(
                    "Remote split part failed index=%s start=%s duration=%s",
                    index,
                    start_time,
                    duration,
                )

    async def update_progress():
        ready = len(generated)
        running = total_parts - ready - len(errors)
        await status.edit_text(
            f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
            f"⏱️ Duration: {format_time(server.duration)}\n"
            f"🧩 Parts: {total_parts}\n"
            f"⚙️ Preparing: {ready}/{total_parts} ready"
            + (f"\n❌ Failed: {len(errors)}" if errors else "")
        )

    try:
        await status.edit_text(
            f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
            f"🧩 Parts: {total_parts}\n"
            "⚡ Preparing parts in parallel..."
        )

        tasks = [asyncio.create_task(build_part(index)) for index in range(total_parts)]
        while True:
            pending = [task for task in tasks if not task.done()]
            await update_progress()
            if not pending:
                break
            await asyncio.sleep(2.0)

        await asyncio.gather(*tasks)

        if errors:
            failed = ", ".join(str(index + 1) for index in sorted(errors))
            raise RuntimeError(f"Parts failed: {failed}")

        await status.edit_text(
            f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
            f"🧩 {total_parts}/{total_parts} parts ready\n"
            "📤 Sending parts in order..."
        )

        for index in range(total_parts):
            part = generated.get(index)
            if part is None:
                raise RuntimeError(f"Part {index + 1} missing.")
            display_index = index + 1
            await status.edit_text(
                f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
                f"🧩 Part {display_index}/{total_parts}\n"
                "📤 Sending..."
            )
            await send_file(
                update,
                part,
                f"✂️ {anime} S{season} E{episode} — Part {display_index}/{total_parts}",
            )
            part.unlink(missing_ok=True)
    finally:
        for part in output_dir.glob("part_*.mp4"):
            part.unlink(missing_ok=True)
        await server.close()


async def run_remote_part(server_url, start_time, duration, output):
    """Extract one remote part through the local Telegram range proxy."""
    await run_command(
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-seekable", "1",
        "-multiple_requests", "1",
        "-initial_request_size", "2M",
        "-request_size", "2M",
        "-short_seek_size", "4M",
        "-ss", str(start_time),
        "-i", server_url,
        "-t", str(duration),
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(output),
    )

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError("Remote split output empty bana hai.")

    stdout, _ = await run_command(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(output),
    )
    try:
        verified_duration = float(stdout.strip())
    except (TypeError, ValueError):
        verified_duration = 0.0
    if verified_duration <= 0.05:
        raise RuntimeError(f"Remote split output duration invalid: {verified_duration}")


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    status = await update.message.reply_text("✂️ SPLIT STARTED\n\n🔎 Finding source...")
    try:
        raw = " ".join(context.args).strip()
        source_match = re.match(r"^(\d+)\s+(.+?)\s+[Ss](\d+)\s+[Ee](\d+)$", raw)
        if source_match:
            part_duration = int(source_match.group(1))
            anime = source_match.group(2).strip()
            season = source_match.group(3)
            episode = source_match.group(4)
            if part_duration <= 0:
                raise ValueError("Split duration positive hona chahiye.")

            source_url = get_best_source(anime, season, episode)
            if not source_url:
                raise ValueError(f"{anime} S{season} E{episode} library me nahi mila.")

            await status.edit_text(
                f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
                f"⏱️ Part size: {part_duration}s\n\n🔌 Connecting to Telegram source..."
            )
            from telethon_runtime import ensure_telethon_client
            from telegram_remote import get_telegram_video_info
            source_client = await ensure_telethon_client()

            await status.edit_text(
                f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
                "📡 Reading episode duration..."
            )
            chat_id, message_id = parse_telegram_message_link(source_url)
            _, total_duration, _ = await get_telegram_video_info(source_client, chat_id, message_id)

            output_dir = user_temp_dir(user_id) / f"split_{re.sub(r'[^A-Za-z0-9_-]+', '_', anime)}_S{season}E{episode}"
            output_dir.mkdir(parents=True, exist_ok=True)
            total_parts = max(1, int((total_duration + part_duration - 0.001) // part_duration))

            await status.edit_text(
                f"✂️ SPLIT\n\n📚 {anime} S{season} E{episode}\n"
                f"⏱️ Duration: {format_time(total_duration)}\n"
                f"🧩 Parts: {total_parts}\n\n"
                "🔌 Opening remote video stream..."
            )

            await _stream_remote_split(
                update,
                status,
                source_client,
                source_url,
                output_dir,
                part_duration,
                total_parts,
                anime,
                season,
                episode,
            )

            shutil.rmtree(output_dir, ignore_errors=True)
            await status.edit_text(
                f"✂️ SPLIT COMPLETE ✅\n\n📚 {anime} S{season} E{episode}\n"
                f"🧩 {total_parts} parts sent.\n"
                "⚡ Parts prepared in parallel and sent in order."
            )
            return

        part_duration = 30
        if context.args and context.args[0].isdigit():
            part_duration = int(context.args[0])
        if part_duration <= 0:
            raise ValueError("Split duration 1 second se zyada hona chahiye.")
        input_path = await _get_active_video(user_id)
        if input_path is None:
            raise ValueError("Pehle original video bhejo.")

        await status.edit_text(
            f"✂️ SPLIT\n\n📱 Active original video\n"
            f"⏱️ Part size: {part_duration}s\n\n⚙️ Splitting..."
        )
        parts = await split_video(input_path, part_duration)
        for index, part in enumerate(parts, start=1):
            await status.edit_text(
                f"✂️ SPLIT\n\n🧩 Sending part {index}/{len(parts)}..."
            )
            await send_file(update, part, f"✂️ Part {index}/{len(parts)}")
            part.unlink(missing_ok=True)
        await status.edit_text(
            f"✂️ SPLIT COMPLETE ✅\n\n🧩 {len(parts)} parts sent."
        )
    except Exception as exc:
        logger.exception("Split failed")
        await status.edit_text(f"❌ SPLIT FAILED\n\n{exc}")


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cleanup_user_temp(user_id)
    await update.message.reply_text("♻️ Current original video reset ho gaya. Naya video bhej sakte ho.")


async def receive_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = update.effective_user.id
    if context.user_data.get("save_step"):
        await message.reply_text("💾 Save process chal raha hai. Abhi requested text/links bhejo.")
        return
    if not message.video and not message.document:
        return
    try:
        cleanup_user_temp(user_id)
        path = await download_bot_video(message, user_id, bot_mtproto_client)
        active_videos[user_id] = str(path)
        await message.reply_text(
            "✅ Original video set ho gaya.\n\n"
            "/clip 01:00 - 01:30\n"
            "/split 30\n"
            "/next"
        )
    except Exception as exc:
        await message.reply_text(f"❌ {exc}")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await process_save_text(update, context):
        return


async def error_handler(update, context):
    logger.exception("Unhandled Telegram error", exc_info=context.error)


async def _run_source_sync():
    try:
        result = await sync_source_library(telethon_client)
        logger.info("AnimeNation012 sync result: %s", result)
    except Exception:
        logger.exception("AnimeNation012 source sync failed")


async def post_init(application: Application):
    global telethon_client, bot_mtproto_client, source_sync_task
    init_db()
    if TG_API_ID and TG_API_HASH:
        telethon_client = TelegramClient(TELEGRAM_SESSION, TG_API_ID, TG_API_HASH)
        await telethon_client.start()
        logger.info("Telethon source client connected.")
        bot_mtproto_client = TelegramClient(MemorySession(), TG_API_ID, TG_API_HASH)
        await bot_mtproto_client.start(bot_token=BOT_TOKEN)
        logger.info("Telethon bot media client connected.")
        source_sync_task = asyncio.create_task(_run_source_sync())
    else:
        logger.warning("TG_API_ID/TG_API_HASH missing. Telegram source features disabled.")


async def post_shutdown(application: Application):
    global telethon_client, bot_mtproto_client, source_sync_task
    if source_sync_task and not source_sync_task.done():
        source_sync_task.cancel()
        try:
            await source_sync_task
        except asyncio.CancelledError:
            pass
    if bot_mtproto_client:
        try:
            await bot_mtproto_client.disconnect()
        except Exception:
            pass
    if telethon_client:
        try:
            await telethon_client.disconnect()
        except Exception:
            pass


def main():
    global _instance_lock_handle
    validate_bot_config()

    # Prevent accidental duplicate bot.py processes from polling Telegram at
    # the same time. The lock is held for the lifetime of this process.
    lock_path = Path(TEMP_DIR) / "bot_instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _instance_lock_handle = lock_path.open("a+")
    try:
        fcntl.flock(_instance_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another bot.py instance is already running; exiting.")
        _instance_lock_handle.close()
        _instance_lock_handle = None
        return

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .get_updates_pool_timeout(30)
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("library", nav_library_command))
    application.add_handler(CallbackQueryHandler(nav_library_callback, pattern=r"^la:|^ls:|^lb$"))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("edit", edit_handler))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("clip", source_clip_command))
    application.add_handler(CommandHandler("clips", source_clip_command))
    application.add_handler(CommandHandler("split", split_command))
    application.add_handler(CommandHandler("next", next_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video))
    application.add_error_handler(error_handler)
    logger.info("Bot starting...")
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            bootstrap_retries=-1,
            drop_pending_updates=False,
        )
    except NetworkError:
        logger.exception("Telegram network connection failed during polling.")
        raise


if __name__ == "__main__":
    main()
