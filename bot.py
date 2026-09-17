import asyncio
import logging
import re
import shutil
from html import escape
from pathlib import Path

from telethon import TelegramClient
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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
)

from find_engine import find_and_build, _extract_remote_clip
from yt_downloader import download_video_from_url
from source_sync import sync_source_library, render_library_html


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("anime-bot")

telethon_client = None
source_sync_task = None
job_lock = asyncio.Lock()
active_videos = {}


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
        await update.message.reply_text(
            f"⚠️ Output {size / 1024 / 1024:.1f} MB hai.\n"
            f"Configured Telegram limit: {TELEGRAM_MAX_BYTES / 1024 / 1024:.0f} MB."
        )
        return

    with path.open("rb") as f:
        await update.message.reply_video(
            video=f,
            caption=caption,
            supports_streaming=True,
        )


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

    args = context.args
    if not args:
        await edit_command(update, context)
        return

    action = args[0].lower()
    try:
        if action == "add":
            if len(args) < 6:
                raise ValueError("Format: /edit add Anime S1 E1 1080p URL")
            anime = args[1]
            season = args[2].lstrip("Ss")
            episode = args[3].lstrip("Ee")
            quality = args[4].lower()
            url = args[5]
            parse_telegram_message_link(url)
            add_source(anime, season, episode, quality, url)
            await update.message.reply_text("✅ Source saved.")
        elif action == "delete":
            if len(args) < 5:
                raise ValueError("Format: /edit delete Anime S1 E1 1080p")
            delete_source(args[1], args[2].lstrip("Ss"), args[3].lstrip("Ee"), args[4].lower())
            await update.message.reply_text("✅ Source deleted.")
        elif action == "delete_episode":
            if len(args) < 4:
                raise ValueError("Format: /edit delete_episode Anime S1 E1")
            delete_episode(args[1], args[2].lstrip("Ss"), args[3].lstrip("Ee"))
            await update.message.reply_text("✅ Episode deleted.")
        elif action == "delete_season":
            if len(args) < 3:
                raise ValueError("Format: /edit delete_season Anime S1")
            delete_season(args[1], args[2].lstrip("Ss"))
            await update.message.reply_text("✅ Season deleted.")
        elif action == "delete_anime":
            if len(args) < 2:
                raise ValueError("Format: /edit delete_anime Anime")
            with get_connection() as conn:
                conn.execute("DELETE FROM library WHERE LOWER(anime) = LOWER(?)", (args[1],))
                conn.commit()
            await update.message.reply_text("✅ Anime deleted from library.")
        else:
            raise ValueError("Unknown edit action.")
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
            if telethon_client is None:
                raise ValueError("Telegram source client connected nahi hai.")
            source_mode = True
            input_path = None
        else:
            raise ValueError(
                "Usage:\n/clip 01:20 - 01:50\n"
                "or\n/clips Anime S1 E1 01:20 - 01:50"
            )

        if source_mode:
            output = unique_source_clip_path(user_id, anime, season, episode)
            await _extract_remote_clip(telethon_client, source_url, start, end, output)
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


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        args = context.args
        part_duration = 30
        if args and args[0].isdigit():
            part_duration = int(args[0])
        if part_duration <= 0:
            raise ValueError("Split duration 1 second se zyada hona chahiye.")
        input_path = await _get_active_video(user_id)
        if input_path is None:
            raise ValueError("Pehle original video bhejo.")
        parts = await split_video(input_path, part_duration)
        for index, part in enumerate(parts, start=1):
            await send_file(update, part, f"✂️ Part {index}/{len(parts)}")
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")


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
        path = await download_bot_video(message, user_id)
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
    global telethon_client, source_sync_task
    init_db()
    if TG_API_ID and TG_API_HASH:
        telethon_client = TelegramClient(TELEGRAM_SESSION, TG_API_ID, TG_API_HASH)
        await telethon_client.start()
        logger.info("Telethon source client connected.")
        source_sync_task = asyncio.create_task(_run_source_sync())
    else:
        logger.warning("TG_API_ID/TG_API_HASH missing. Telegram source features disabled.")


async def post_shutdown(application: Application):
    global telethon_client, source_sync_task
    if source_sync_task and not source_sync_task.done():
        source_sync_task.cancel()
        try:
            await source_sync_task
        except asyncio.CancelledError:
            pass
    if telethon_client:
        try:
            await telethon_client.disconnect()
        except Exception:
            pass


def main():
    validate_bot_config()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("library", library_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("edit", edit_handler))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("clip", clip_command))
    application.add_handler(CommandHandler("clips", clip_command))
    application.add_handler(CommandHandler("split", split_command))
    application.add_handler(CommandHandler("next", next_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, receive_video))
    application.add_error_handler(error_handler)
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
