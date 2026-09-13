import asyncio
import logging
import re
import shutil
from pathlib import Path
from datetime import datetime

from telethon import TelegramClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

from config import (
    BOT_TOKEN,
    TEMP_DIR,
    TELEGRAM_MAX_BYTES,
    validate_bot_config,
    TG_API_ID,
    TG_API_HASH,
    TELEGRAM_SESSION,
    OWNER_ID,
)

from ffmpeg_utils import get_duration, make_clip, split_video, parse_time
from progress import ProgressTracker
from telegram_media import download_bot_video
from permissions import require_owner
from library import (
    format_anime_list,
    format_season_list,
    format_episode_list,
    format_quality_list,
    format_source_links,
    LibraryNavigation,
)
from database import (
    get_animes,
    get_seasons,
    get_episodes,
    get_qualities,
    get_source_url,
    get_all_sources_for_episode,
    add_source,
    seed_naruto,
)
from gemini_analyzer import (
    analyze_youtube_short_async,
    align_segments_with_library,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("telegram-video-bot")

active_videos = {}
job_lock = asyncio.Lock()
telethon_client = None

# Navigation state per user
user_nav = {}


def user_temp_dir(user_id):
    path = Path(TEMP_DIR) / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_user_temp(user_id):
    path = user_temp_dir(user_id)
    try:
        shutil.rmtree(path)
    except Exception as exc:
        logger.warning("Cleanup failed: %s", exc)


def get_nav(user_id) -> LibraryNavigation:
    if user_id not in user_nav:
        user_nav[user_id] = LibraryNavigation()
    return user_nav[user_id]


async def send_output(update, output_path, caption="✅ Done!"):
    size = output_path.stat().st_size
    if size <= TELEGRAM_MAX_BYTES:
        with output_path.open("rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
            )
    else:
        await update.message.reply_text(
            f"⚠️ File {size / (1024 * 1024):.2f} MB (max 50 MB)\n"
            "Google Drive upload coming soon."
        )


async def start_command(update, context):
    """Start command - fresh interface."""
    await update.message.reply_text(
        "🎬 Anime Video Bot - Redesigned\n\n"
        "Commands:\n"
        "/help - Full documentation\n"
        "/library - Browse anime collection\n"
        "/save - Add new anime episodes\n"
        "/edit - Modify existing sources\n"
        "/find - Extract clips from YouTube Shorts\n\n"
        "Video tools:\n"
        "/clip 30 - 50 - Cut video segment\n"
        "/split 30 - Split into 30s parts"
    )


async def help_command(update, context):
    """Help command."""
    await update.message.reply_text(
        "📖 HELP\n\n"
        "📚 LIBRARY\n"
        "/library - Browse anime (text navigation)\n\n"
        "💾 SAVE SOURCES\n"
        "/save Naruto - Start interactive save\n"
        "  Bot asks: season count, qualities, URLs\n\n"
        "✏️ EDIT SOURCES\n"
        "/edit - Add/modify single source\n\n"
        "🎯 FIND CLIPS\n"
        "/find <YouTube Short URL> - Extract segments\n"
        "  Gemini analyzes all source anime\n"
        "  Bot extracts exact clips\n"
        "  Returns merged video with captions\n\n"
        "✂️ VIDEO TOOLS\n"
        "/clip - Cut/clip video\n"
        "/split - Split video into parts"
    )


async def library_command(update, context):
    """Browse library - text navigation."""
    user_id = update.effective_user.id
    nav = get_nav(user_id)
    
    animes = get_animes()
    if not animes:
        await update.message.reply_text("❌ No anime in library. Use /save to add.")
        return
    
    text = format_anime_list(animes)
    
    # Create buttons for each anime
    buttons = []
    for anime in animes:
        buttons.append(
            [InlineKeyboardButton(text=anime, callback_data=f"anime:{anime}")]
        )
    buttons.append([InlineKeyboardButton(text="❌ Close", callback_data="close")])
    
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(text, reply_markup=reply_markup)


async def button_callback(update, context):
    """Handle library navigation buttons."""
    query = update.callback_query
    user_id = update.effective_user.id
    nav = get_nav(user_id)
    data = query.data
    
    if data == "close":
        await query.answer()
        await query.edit_message_text("❌ Closed")
        return
    
    # Parse callback data
    if data.startswith("anime:"):
        anime = data[6:]
        nav.set_anime(anime)
        seasons = get_seasons(anime)
        
        if not seasons:
            await query.answer("No seasons found")
            return
        
        text = format_season_list(anime, seasons)
        buttons = [
            [InlineKeyboardButton(text=f"Season {s}", callback_data=f"season:{s}")]
            for s in seasons
        ]
        buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data.startswith("season:"):
        season = data[7:]
        nav.set_season(season)
        anime = nav.current_anime
        episodes = get_episodes(anime, season)
        
        if not episodes:
            await query.answer("No episodes found")
            return
        
        text = format_episode_list(anime, season, episodes)
        buttons = [
            [InlineKeyboardButton(text=f"Episode {e}", callback_data=f"episode:{e}")]
            for e in episodes[:20]  # Limit to 20
        ]
        buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data.startswith("episode:"):
        episode = data[8:]
        nav.set_episode(episode)
        anime = nav.current_anime
        season = nav.current_season
        
        sources = get_all_sources_for_episode(anime, season, episode)
        text = format_source_links(anime, season, episode, sources)
        
        buttons = [
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"season:{season}")]
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    elif data == "back":
        state = nav.get_state()
        if state["episode"]:
            # Back to season
            season = nav.current_season
            anime = nav.current_anime
            episodes = get_episodes(anime, season)
            text = format_episode_list(anime, season, episodes)
            buttons = [
                [InlineKeyboardButton(text=f"Episode {e}", callback_data=f"episode:{e}")]
                for e in episodes[:20]
            ]
            nav.current_episode = None
        elif state["season"]:
            # Back to anime
            anime = nav.current_anime
            seasons = get_seasons(anime)
            text = format_season_list(anime, seasons)
            buttons = [
                [InlineKeyboardButton(text=f"Season {s}", callback_data=f"season:{s}")]
                for s in seasons
            ]
            nav.current_season = None
        else:
            # Back to list
            animes = get_animes()
            text = format_anime_list(animes)
            buttons = [
                [InlineKeyboardButton(text=anime, callback_data=f"anime:{anime}")]
                for anime in animes
            ]
            nav.reset()
        
        buttons.append([InlineKeyboardButton(text="❌ Close", callback_data="close")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    
    await query.answer()


async def save_command(update, context):
    """Save anime sources - owner only."""
    user_id = update.effective_user.id
    try:
        require_owner(user_id)
    except PermissionError:
        await update.message.reply_text("❌ Owner only")
        return
    
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "💾 Usage: /save Naruto\n\n"
            "Bot will ask for:\n"
            "1. Number of seasons\n"
            "2. Qualities (480p, 720p, 1080p)\n"
            "3. Source URLs (one per episode)"
        )
        return
    
    # Start save session
    context.user_data["save_anime"] = text
    context.user_data["save_step"] = "seasons"
    
    await update.message.reply_text(
        f"💾 Saving: {text}\n\n"
        "How many seasons? (1-10)"
    )


async def edit_command(update, context):
    """Edit existing sources - owner only."""
    user_id = update.effective_user.id
    try:
        require_owner(user_id)
    except PermissionError:
        await update.message.reply_text("❌ Owner only")
        return
    
    await update.message.reply_text(
        "✏️ EDIT SOURCES\n\n"
        "Format:\n"
        "Anime Season Episode Quality URL\n\n"
        "Example:\n"
        "Naruto 1 5 720p https://t.me/channel/123"
    )
    context.user_data["edit_mode"] = True


async def find_command(update, context):
    """Find clips from YouTube Short using Gemini."""
    user_id = update.effective_user.id
    
    # For now, ask for video upload since we need the actual video
    await update.message.reply_text(
        "🎯 FIND CLIPS\n\n"
        "Send a video/YouTube Short.\n"
        "Gemini will analyze and extract anime segments.\n\n"
        "Processing will:\n"
        "1️⃣ Identify all anime scenes\n"
        "2️⃣ Match against saved sources\n"
        "3️⃣ Extract exact clips with FFmpeg\n"
        "4️⃣ Merge in order with captions"
    )


async def receive_video(update, context):
    """Handle uploaded videos."""
    message = update.message
    user = update.effective_user
    user_id = user.id
    
    if not message.video:
        return
    
    # Check if in edit mode
    if context.user_data.get("edit_mode"):
        await update.message.reply_text("❌ Send text command for edit, not video")
        return
    
    # Otherwise treat as input for /find
    status = await message.reply_text("📥 Video received\n⏳ Analyzing with Gemini...")
    
    try:
        # Download video
        video_path = await download_bot_video(message, user_id)
        
        # Analyze with Gemini
        analysis = await analyze_youtube_short_async(str(video_path))
        
        if analysis["error"]:
            await status.edit_text(f"❌ Analysis failed: {analysis['error']}")
            return
        
        segments = analysis.get("segments", [])
        if not segments:
            await status.edit_text("❌ No anime segments detected")
            return
        
        # Align with library
        def lookup_fn(anime, season, episode):
            return get_source_url(anime, season, episode, "1080p") or \
                   get_source_url(anime, season, episode, "720p") or \
                   get_source_url(anime, season, episode, "480p")
        
        aligned = align_segments_with_library(segments, lookup_fn)
        
        # Display results
        result_text = "🎯 ANALYSIS RESULTS\n\n"
        found_count = sum(1 for s in aligned if s.get("found"))
        result_text += f"Segments: {len(aligned)}\nMatched: {found_count}\n\n"
        
        for i, seg in enumerate(aligned, 1):
            result_text += (
                f"{i}. {seg.get('anime', '?')} S{seg.get('season', '?')}"
                f"E{seg.get('episode', '?')}\n"
                f"   Time: {seg['start_time']:.1f}s - {seg['end_time']:.1f}s\n"
                f"   Found: {'✅' if seg.get('found') else '❌'}\n\n"
            )
        
        await status.edit_text(result_text)
        
    except Exception as e:
        logger.exception("Find command error")
        await status.edit_text(f"❌ Error: {e}")
    finally:
        try:
            Path(video_path).unlink(missing_ok=True)
        except Exception:
            pass


async def text_handler(update, context):
    """Handle text input (edit mode, save mode)."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    
    # Save mode
    if context.user_data.get("save_step") == "seasons":
        if not text.isdigit() or not 1 <= int(text) <= 10:
            await update.message.reply_text("❌ Enter 1-10")
            return
        context.user_data["save_seasons"] = int(text)
        context.user_data["save_step"] = "qualities"
        await update.message.reply_text(
            "📹 Qualities? (480p, 720p, 1080p)\n"
            "Example: 480p 720p 1080p"
        )
        return
    
    if context.user_data.get("save_step") == "qualities":
        qualities = text.lower().split()
        if not qualities:
            await update.message.reply_text("❌ Enter at least one quality")
            return
        context.user_data["save_qualities"] = qualities
        context.user_data["save_step"] = "urls"
        await update.message.reply_text(
            f"🔗 Send {context.user_data['save_seasons'] * len(qualities)} URLs\n"
            "(One per line)"
        )
        return
    
    if context.user_data.get("save_step") == "urls":
        urls = text.split('\n')
        urls = [u.strip() for u in urls if u.strip()]
        
        if not urls:
            await update.message.reply_text("❌ No URLs found")
            return
        
        # Save to database
        anime = context.user_data["save_anime"]
        seasons = context.user_data["save_seasons"]
        qualities = context.user_data["save_qualities"]
        
        try:
            # Distribute URLs across seasons and episodes
            episodes_per_season = len(urls) // seasons
            
            for season in range(1, seasons + 1):
                for ep in range(1, episodes_per_season + 1):
                    for quality in qualities:
                        url_idx = (season - 1) * episodes_per_season + ep - 1
                        if url_idx < len(urls):
                            add_source(anime, str(season), str(ep), quality, urls[url_idx])
            
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Saved!\n"
                f"🎬 {anime}\n"
                f"📚 {seasons} seasons\n"
                f"📹 {', '.join(qualities)}\n"
                f"🔗 {len(urls)} sources"
            )
        except Exception as e:
            logger.exception("Save error")
            await update.message.reply_text(f"❌ Save failed: {e}")
        
        return
    
    # Edit mode
    if context.user_data.get("edit_mode"):
        parts = text.split()
        if len(parts) < 5:
            await update.message.reply_text(
                "❌ Format: Anime Season Episode Quality URL"
            )
            return
        
        anime, season, episode, quality = parts[0], parts[1], parts[2], parts[3]
        url = parts[4]
        
        try:
            add_source(anime, season, episode, quality, url)
            await update.message.reply_text(f"✅ Updated: {anime} S{season}E{episode} {quality}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        
        return


async def clip_command(update, context):
    """Cut video clip."""
    user = update.effective_user
    user_id = user.id
    active = active_videos.get(user_id)
    
    if not active:
        await update.message.reply_text("❌ Send video first")
        return
    
    text = update.message.text or ""
    args = text.partition(" ")[2].strip()
    
    if not args:
        await update.message.reply_text(
            "✂️ Format: /clip 30 - 50\n"
            "or /clip 02:30 - 04:15"
        )
        return
    
    try:
        # Simple parsing
        parts = args.split("-")
        if len(parts) != 2:
            raise ValueError("Use format: START - END")
        
        start = parse_time(parts[0].strip())
        end = parse_time(parts[1].strip())
        
        if end <= start:
            raise ValueError("End > Start")
        
        status = await update.message.reply_text("⏳ Creating clip...")
        
        output = user_temp_dir(user_id) / "clip.mp4"
        await make_clip(Path(active["path"]), start, end, str(output))
        
        await send_output(
            update,
            output,
            caption=f"✂️ Clip {start:.1f}s - {end:.1f}s"
        )
        output.unlink(missing_ok=True)
        
    except Exception as e:
        logger.exception("Clip error")
        await update.message.reply_text(f"❌ Error: {e}")


async def split_command(update, context):
    """Split video."""
    user = update.effective_user
    user_id = user.id
    active = active_videos.get(user_id)
    
    if not active:
        await update.message.reply_text("❌ Send video first")
        return
    
    text = update.message.text or ""
    args = text.split(maxsplit=1)
    
    if len(args) < 2:
        await update.message.reply_text("✂️ Format: /split 30")
        return
    
    try:
        duration = int(args[1])
        if duration <= 0:
            raise ValueError("Duration > 0")
        
        status = await update.message.reply_text("⏳ Splitting...")
        
        files = await split_video(Path(active["path"]), duration)
        
        for f in files:
            await send_output(update, Path(f), f"✂️ Part")
            Path(f).unlink(missing_ok=True)
        
    except Exception as e:
        logger.exception("Split error")
        await update.message.reply_text(f"❌ Error: {e}")


async def telegram_client_start(app):
    global telethon_client
    if TG_API_ID and TG_API_HASH:
        telethon_client = TelegramClient(TELEGRAM_SESSION, TG_API_ID, TG_API_HASH)
        await telethon_client.start()
        logger.info("Telethon connected")


async def telegram_client_stop(app):
    global telethon_client
    if telethon_client:
        await telethon_client.disconnect()


def main():
    validate_bot_config()
    
    # Seed Naruto on first run
    try:
        animes = get_animes()
        if not animes:
            logger.info("Seeding Naruto...")
            seed_naruto()
    except Exception as e:
        logger.warning(f"Seed failed: {e}")
    
    Path(TEMP_DIR).mkdir(parents=True, exist_ok=True)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("library", library_command))
    app.add_handler(CommandHandler("save", save_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("clip", clip_command))
    app.add_handler(CommandHandler("split", split_command))
    
    app.add_handler(CallbackQueryHandler(button_callback))
    
    app.add_handler(MessageHandler(filters.VIDEO, receive_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    app.add_post_init_job(telegram_client_start)
    app.add_post_stop_job(telegram_client_stop)
    
    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
