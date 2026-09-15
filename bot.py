import asyncio
import logging
import re
import shutil
from pathlib import Path

from telethon import TelegramClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    anime_exists,
    delete_source,
    delete_episode,
    delete_season,
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

from find_engine import find_and_build
from yt_downloader import download_video_from_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("anime-bot")

telethon_client = None
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


def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and int(user_id) == int(OWNER_ID)


async def send_file(update: Update, path: Path, caption: str):
    size = path.stat().st_size

    if size > TELEGRAM_MAX_BYTES:
        await update.message.reply_text(
            f"⚠️ Output {size / 1024 / 1024:.1f} MB hai.\n"
            f"Telegram limit configured: "
            f"{TELEGRAM_MAX_BYTES / 1024 / 1024:.0f} MB."
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
        "📚 Library:\n"
        "/save AnimeName\n"
        "/library\n"
        "/edit\n\n"
        "🎯 Finder:\n"
        "/find <YouTube URL>\n\n"
        "✂️ Video tools:\n"
        "/clips Anime S1 E1 01:20 - 01:50\n"
        "/split 30 Anime S1 E1\n\n"
        "Reply-video mode bhi supported hai."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 HELP\n\n"

        "💾 SAVE\n"
        "/save Naruto\n"
        "→ season count\n"
        "→ har season ka episode count\n"
        "→ har episode ki Telegram link, one per line\n\n"

        "Example:\n"
        "/save Naruto\n"
        "2\n"
        "220\n"
        "https://t.me/channel/101\n"
        "https://t.me/channel/102\n"
        "...\n\n"

        "🎯 FIND\n"
        "/find https://youtube.com/shorts/xxxxx\n"
        "→ video download\n"
        "→ Gemini scene analysis\n"
        "→ saved Telegram episode source\n"
        "→ visual timestamp matching\n"
        "→ exact clips\n"
        "→ original order me merge\n\n"

        "✂️ CLIPS\n"
        "/clips Naruto S1 E1 01:20 - 01:50\n"
        "Reply to a video:\n"
        "/clips 01:20 - 01:50\n\n"

        "✂️ SPLIT\n"
        "/split 30 Naruto S1 E1\n"
        "Default: 30 seconds\n"
        "Reply video:\n"
        "/split 30\n\n"

        "✏️ EDIT\n"
        "/edit"
    )


async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    animes = get_animes()

    if not animes:
        await update.message.reply_text(
            "📚 Library empty hai.\n/save AnimeName se start karo."
        )
        return

    buttons = [
        [InlineKeyboardButton(a, callback_data=f"anime|{a}")]
        for a in animes
    ]

    await update.message.reply_text(
        "📚 ANIME LIBRARY\n\n"
        + "\n".join(f"• {a}" for a in animes),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def library_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("anime|"):
        anime = data.split("|", 1)[1]
        seasons = get_seasons(anime)

        buttons = [
            [
                InlineKeyboardButton(
                    f"Season {s}",
                    callback_data=f"season|{anime}|{s}",
                )
            ]
            for s in seasons
        ]

        await query.edit_message_text(
            f"📺 {anime}\n\n"
            + "\n".join(f"• Season {s}" for s in seasons),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("season|"):
        _, anime, season = data.split("|", 2)

        episodes = get_episodes(anime, season)

        buttons = [
            [
                InlineKeyboardButton(
                    f"Episode {e}",
                    callback_data=f"episode|{anime}|{season}|{e}",
                )
            ]
            for e in episodes[:100]
        ]

        await query.edit_message_text(
            f"📺 {anime} S{season}\n\n"
            f"Episodes: {len(episodes)}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("episode|"):
        _, anime, season, episode = data.split("|", 3)

        sources = get_all_sources_for_episode(
            anime,
            season,
            episode,
        )

        if not sources:
            text = "❌ Source nahi mila."
        else:
            text = (
                f"📺 {anime} S{season} E{episode}\n\n"
                + "\n".join(
                    f"• {quality}: {url}"
                    for quality, url in sources.items()
                )
            )

        await query.edit_message_text(text)


async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_owner(user_id):
        await update.message.reply_text("❌ Owner only.")
        return

    anime = " ".join(context.args).strip()

    if not anime:
        await update.message.reply_text(
            "Usage:\n/save Naruto"
        )
        return

    context.user_data.clear()

    context.user_data["save_anime"] = anime
    context.user_data["save_step"] = "season_count"

    await update.message.reply_text(
        f"💾 Saving: {anime}\n\n"
        "Kitne seasons hain?\n"
        "Example: 3"
    )


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Owner only.")
        return

    await update.message.reply_text(
        "✏️ EDIT\n\n"

        "Source replace/add:\n"
        "/edit add Naruto S1 E5 720p https://t.me/channel/123\n\n"

        "Source delete:\n"
        "/edit delete Naruto S1 E5 720p\n\n"

        "Episode delete:\n"
        "/edit delete_episode Naruto S1 E5\n\n"

        "Season delete:\n"
        "/edit delete_season Naruto S1"
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
                raise ValueError(
                    "Format: /edit add Anime S1 E1 720p URL"
                )

            anime = args[1]
            season = args[2].lstrip("Ss")
            episode = args[3].lstrip("Ee")
            quality = args[4]
            url = args[5]

            parse_telegram_message_link(url)

            add_source(
                anime,
                season,
                episode,
                quality,
                url,
            )

            await update.message.reply_text(
                "✅ Source saved."
            )

        elif action == "delete":
            if len(args) < 5:
                raise ValueError(
                    "Format: /edit delete Anime S1 E1 720p"
                )

            anime = args[1]
            season = args[2].lstrip("Ss")
            episode = args[3].lstrip("Ee")
            quality = args[4]

            delete_source(
                anime,
                season,
                episode,
                quality,
            )

            await update.message.reply_text(
                "✅ Source deleted."
            )

        elif action == "delete_episode":
            if len(args) < 4:
                raise ValueError(
                    "Format: /edit delete_episode Anime S1 E1"
                )

            anime = args[1]
            season = args[2].lstrip("Ss")
            episode = args[3].lstrip("Ee")

            delete_episode(
                anime,
                season,
                episode,
            )

            await update.message.reply_text(
                "✅ Episode deleted."
            )

        elif action == "delete_season":
            if len(args) < 3:
                raise ValueError(
                    "Format: /edit delete_season Anime S1"
                )

            anime = args[1]
            season = args[2].lstrip("Ss")

            delete_season(
                anime,
                season,
            )

            await update.message.reply_text(
                "✅ Season deleted."
            )

        else:
            raise ValueError("Unknown edit action.")

    except Exception as exc:
        await update.message.reply_text(
            f"❌ {exc}"
        )


async def process_save_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    step = context.user_data.get("save_step")

    if not step:
        return False

    text = (update.message.text or "").strip()

    if step == "season_count":
        if not text.isdigit():
            await update.message.reply_text(
                "❌ Sirf number bhejo."
            )
            return True

        count = int(text)

        if count < 1 or count > 50:
            await update.message.reply_text(
                "❌ Seasons 1-50 ke beech rakho."
            )
            return True

        context.user_data["save_season_count"] = count
        context.user_data["save_current_season"] = 1
        context.user_data["save_step"] = "episode_count"

        await update.message.reply_text(
            "📺 Season 1 me kitne episodes hain?"
        )

        return True

    if step == "episode_count":
        if not text.isdigit():
            await update.message.reply_text(
                "❌ Episode count number me bhejo."
            )
            return True

        count = int(text)

        if count < 1 or count > 5000:
            await update.message.reply_text(
                "❌ Episode count invalid."
            )
            return True

        season = context.user_data["save_current_season"]

        context.user_data["save_episode_count"] = count
        context.user_data["save_step"] = "episode_links"

        await update.message.reply_text(
            f"🔗 Season {season}: {count} Telegram links bhejo.\n\n"
            "Har line = ek episode.\n"
            "Pehli line = Episode 1\n"
            "Dusri line = Episode 2\n"
            "...\n\n"
            "Example:\n"
            "https://t.me/animeclipcutter/3/5\n"
            "https://t.me/animeclipcutter/3/6"
        )

        return True

    if step == "episode_links":
        links = [
            x.strip()
            for x in text.splitlines()
            if x.strip()
        ]

        expected = context.user_data["save_episode_count"]

        if len(links) != expected:
            await update.message.reply_text(
                f"❌ {expected} links chahiye the.\n"
                f"Aapne {len(links)} bheje."
            )
            return True

        anime = context.user_data["save_anime"]
        season = context.user_data["save_current_season"]

        status = await update.message.reply_text(
            f"⏳ Season {season} validate/save ho raha hai..."
        )

        saved = 0

        for index, url in enumerate(links, start=1):
            try:
                parse_telegram_message_link(url)

                add_source(
                    anime,
                    season,
                    index,
                    "auto",
                    url,
                )

                saved += 1

            except Exception as exc:
                logger.warning(
                    "Invalid source %s: %s",
                    url,
                    exc,
                )

        total_seasons = context.user_data["save_season_count"]

        if season < total_seasons:
            context.user_data["save_current_season"] = season + 1
            context.user_data["save_step"] = "episode_count"

            await status.edit_text(
                f"✅ Season {season}: {saved}/{expected} saved.\n\n"
                f"📺 Ab Season {season + 1} me kitne episodes hain?"
            )

        else:
            anime_name = context.user_data["save_anime"]

            context.user_data.clear()

            await status.edit_text(
                f"🎉 SAVE COMPLETE\n\n"
                f"Anime: {anime_name}\n"
                f"Seasons: {total_seasons}\n"
                f"Last season saved: {saved}/{expected}"
            )

        return True

    return False


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/find https://youtube.com/shorts/xxxxx"
        )
        return

    url = context.args[0]

    if not re.match(
        r"^https?://",
        url,
        re.IGNORECASE,
    ):
        await update.message.reply_text(
            "❌ Valid URL bhejo."
        )
        return

    if job_lock.locked():
        await update.message.reply_text(
            "⏳ Ek find job already chal raha hai. "
            "Pehle uske complete hone ka wait karo."
        )
        return

    status = await update.message.reply_text(
        "🎯 FIND STARTED\n\n"
        "1️⃣ Video download ho raha hai..."
    )

    user_id = update.effective_user.id

    try:
        async with job_lock:
            video_path = await download_video_from_url(
                url,
                user_id,
            )

            await status.edit_text(
                "🎯 FIND\n\n"
                "1️⃣ Video downloaded ✅\n"
                "2️⃣ Gemini scene analysis..."
            )

            result = await find_and_build(
                input_video=video_path,
                user_id=user_id,
                telethon_client=telethon_client,
                progress_message=status,
            )

            output = result["output"]

            await status.edit_text(
                "🎯 FIND\n\n"
                f"Scenes matched: {result['matched']}\n"
                "3️⃣ Clips merged ✅\n"
                "4️⃣ Sending result..."
            )

            await send_file(
                update,
                output,
                "🎬 Exact matched clips",
            )

    except Exception as exc:
        logger.exception("Find failed")
        await status.edit_text(
            f"❌ FIND FAILED\n\n{exc}"
        )

    finally:
        cleanup_user_temp(user_id)


async def clips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        args = context.args

        input_path = None
        cleanup_input = False

        if update.message.reply_to_message:
            replied = update.message.reply_to_message

            if not replied.video and not replied.document:
                raise ValueError(
                    "Reply kisi video message par karo."
                )

            input_path = await download_bot_video(
                replied,
                user_id,
            )

            cleanup_input = True

            if len(args) != 3:
                raise ValueError(
                    "Reply mode:\n/clips 01:20 - 01:50"
                )

            start = parse_time(args[0])
            end = parse_time(args[2])

        else:
            if len(args) != 6:
                raise ValueError(
                    "Format:\n"
                    "/clips Anime S1 E1 01:20 - 01:50"
                )

            anime = args[0]
            season = args[1].lstrip("Ss")
            episode = args[2].lstrip("Ee")

            start = parse_time(args[3])
            end = parse_time(args[5])

            source = get_best_source(
                anime,
                season,
                episode,
            )

            if not source:
                raise ValueError(
                    "Episode source nahi mila."
                )

            if telethon_client is None:
                raise ValueError(
                    "Telegram source client connected nahi hai."
                )

            chat, message_id = parse_telegram_message_link(
                source
            )

            input_path = await download_telethon_message(
                telethon_client,
                chat,
                message_id,
                user_id,
            )

        duration = await get_duration(input_path)

        if start < 0 or end > duration:
            raise ValueError(
                f"Video duration {format_time(duration)} hai."
            )

        output = await make_clip(
            input_path,
            start,
            end,
            name="clip",
        )

        await send_file(
            update,
            output,
            f"✂️ {format_time(start)} → {format_time(end)}",
        )

    except Exception as exc:
        await update.message.reply_text(
            f"❌ {exc}"
        )

    finally:
        if cleanup_input:
            cleanup_user_temp(user_id)


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        args = context.args

        part_duration = 30

        if args and args[0].isdigit():
            part_duration = int(args[0])
            args = args[1:]

        input_path = None

        if update.message.reply_to_message:
            replied = update.message.reply_to_message

            if not replied.video and not replied.document:
                raise ValueError(
                    "Reply kisi video par karo."
                )

            input_path = await download_bot_video(
                replied,
                user_id,
            )

        else:
            if len(args) < 3:
                raise ValueError(
                    "Format:\n"
                    "/split 30 Anime S1 E1"
                )

            anime = args[0]
            season = args[1].lstrip("Ss")
            episode = args[2].lstrip("Ee")

            source = get_best_source(
                anime,
                season,
                episode,
            )

            if not source:
                raise ValueError(
                    "Episode source nahi mila."
                )

            if telethon_client is None:
                raise ValueError(
                    "Telegram source client connected nahi hai."
                )

            chat, message_id = parse_telegram_message_link(
                source
            )

            input_path = await download_telethon_message(
                telethon_client,
                chat,
                message_id,
                user_id,
            )

        parts = await split_video(
            input_path,
            part_duration,
        )

        for index, part in enumerate(parts, start=1):
            await send_file(
                update,
                part,
                f"✂️ Part {index}/{len(parts)}",
            )

    except Exception as exc:
        await update.message.reply_text(
            f"❌ {exc}"
        )

    finally:
        cleanup_user_temp(user_id)


async def receive_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message
    user_id = update.effective_user.id

    if context.user_data.get("save_step"):
        await message.reply_text(
            "💾 Save process chal raha hai. "
            "Abhi requested links/text bhejo."
        )
        return

    if not message.video and not message.document:
        return

    path = await download_bot_video(
        message,
        user_id,
    )

    active_videos[user_id] = str(path)

    await message.reply_text(
        "✅ Video received.\n\n"
        "Ab use kar sakte ho:\n"
        "/split 30\n"
        "/clips 01:00 - 01:30"
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if await process_save_text(update, context):
        return


async def error_handler(update, context):
    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


async def post_init(application: Application):
    global telethon_client

    init_db()

    if TG_API_ID and TG_API_HASH:
        telethon_client = TelegramClient(
            TELEGRAM_SESSION,
            TG_API_ID,
            TG_API_HASH,
        )

        await telethon_client.start()

        logger.info(
            "Telethon source client connected."
        )
    else:
        logger.warning(
            "TG_API_ID/TG_API_HASH missing. "
            "Telegram source downloading disabled."
        )


async def post_shutdown(application: Application):
    global telethon_client

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

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("library", library_command)
    )

    application.add_handler(
        CommandHandler("save", save_command)
    )

    application.add_handler(
        CommandHandler("edit", edit_handler)
    )

    application.add_handler(
        CommandHandler("find", find_command)
    )

    application.add_handler(
        CommandHandler("clips", clips_command)
    )

    application.add_handler(
        CommandHandler("split", split_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            library_callback,
            pattern=r"^(anime|season|episode)\|",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            receive_video,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()