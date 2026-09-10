import asyncio
from matcher import match_findclip, format_match
import logging
import re
import shutil
from pathlib import Path
from datetime import datetime
import uuid
from telethon import TelegramClient
from config import TG_API_ID, TG_API_HASH, TELEGRAM_SESSION

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    BOT_TOKEN,
    TEMP_DIR,
    TELEGRAM_MAX_BYTES,
    validate_bot_config,
)

from ffmpeg_utils import get_duration, make_clip, split_video, parse_time
from progress import ProgressTracker
from telegram_media import (
    download_bot_video,
    parse_telegram_message_link,
    download_telethon_message,
)
from permissions import require_owner
from database import update_library_item, get_batch, get_pending_batch, get_connection
from library import save_episode
from utils import safe_filename, ensure_dir
from library import create_episode
from config import INDEX_DIR
from findclip_engine import (
    SOURCE_DIR,
    index_path_for,
    source_path_for,
    build_index_async,
    load_index,
    search_index_async,
    continuous_match,
    create_clip_async,
)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("telegram-video-bot")

active_videos = {}
job_lock = asyncio.Lock()

# Interactive /save session state
save_sessions = {}

# Telegram user client used only for fetching source links temporarily
telethon_client = None



def user_temp_dir(user_id):
    path = Path(TEMP_DIR) / str(user_id)
    ensure_dir(path)
    return path


def clear_user_temp(user_id):
    path = user_temp_dir(user_id)

    for item in path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except Exception as exc:
            logger.warning("Cleanup failed: %s | %s", item, exc)


def clear_active_video(user_id):
    data = active_videos.pop(user_id, None)

    if not data:
        return

    original = data.get("path")

    if original:
        try:
            Path(original).unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete original: %s", exc)


def parse_range(text):
    pattern = re.compile(
        r"^\s*([0-9]+(?::[0-9]{1,2}){0,2})"
        r"\s*[-–—]\s*"
        r"([0-9]+(?::[0-9]{1,2}){0,2})\s*$"
    )

    match = pattern.match(text)

    if not match:
        raise ValueError(
            "Invalid timing.\n\n"
            "Example:\n"
            "/clip 30 - 50\n"
            "/clip 02:30 - 04:15"
        )

    start = parse_time(match.group(1))
    end = parse_time(match.group(2))

    if end <= start:
        raise ValueError("End time must be greater than start time.")

    return start, end


def parse_multiple_ranges(text):
    ranges = []

    for line in text.splitlines():
        line = line.strip()

        if line:
            ranges.append(parse_range(line))

    if not ranges:
        raise ValueError("No valid timing range found.")

    return ranges


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
            "⚠️ Output file 50 MB se bada hai.\n"
            "Google Drive upload next phase me connect hoga.\n\n"
            f"File size: {size / (1024 * 1024):.2f} MB"
        )


async def start_command(update, context):
    await update.message.reply_text(
        "🎬 Anime Video Bot\n\n"
        "Video bhejo aur phir:\n\n"
        "/clip 30 - 50\n"
        "/clip 02:30 - 04:15\n\n"
        "Multiple clips:\n"
        "00:40 - 01:05\n"
        "03:45 - 04:10\n\n"
        "Split:\n"
        "/split 30\n"
        "/split 30 12:30\n\n"
        "/next = current video clear"
    )


async def help_command(update, context):
    await update.message.reply_text(
        "📖 Commands\n\n"
        "🎥 Pehle video send karo.\n\n"
        "Clip:\n"
        "/clip 30 - 50\n"
        "/clip 02:30 - 04:15\n\n"
        "Multiple clips:\n"
        "/clip\n"
        "00:40 - 01:05\n"
        "03:45 - 04:10\n\n"
        "Split:\n"
        "/split 30\n"
        "/split 30 12:30\n"
        "/split 30 12:30 - 20:52\n\n"
        "/next = current original video clear"
    )


async def receive_video(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    user_id = user.id

    clear_active_video(user_id)
    clear_user_temp(user_id)

    status = await message.reply_text(
        "📥 Video receive ho gaya...\n⏳ Downloading..."
    )

    tracker = ProgressTracker(status)

    try:
        async with job_lock:
            path = await download_bot_video(message, user_id)

        duration = await get_duration(path)

        active_videos[user_id] = {
            "path": str(path),
            "duration": duration,
            "original_name": (
                message.video.file_name
                if message.video and message.video.file_name
                else "video.mp4"
            ),
        }

        await tracker.success(
            "✅ Original video ready!\n\n"
            f"⏱ Duration: {duration:.2f}s\n\n"
            "Ab /clip ya /split use karo.\n"
            "/next se current video clear kar sakte ho."
        )

        logger.info(
            "Active video set | user=%s | path=%s | duration=%.2f",
            user_id,
            path,
            duration,
        )

    except Exception:
        logger.exception("Video receive error")

        clear_active_video(user_id)
        clear_user_temp(user_id)

        await tracker.error(
            "❌ Video process nahi ho saka.\n"
            "Technical details Termux log me available hain."
        )


async def next_command(update, context):
    user = update.effective_user

    if not user:
        return

    clear_active_video(user.id)
    clear_user_temp(user.id)

    await update.message.reply_text(
        "🗑️ Current video/session clear ho gaya.\n"
        "Ab naya video bhej sakte ho."
    )


async def clip_command(update, context):
    user = update.effective_user

    if not user:
        return

    user_id = user.id
    active = active_videos.get(user_id)

    if not active:
        await update.message.reply_text(
            "❌ Pehle ek video send karo."
        )
        return

    raw = update.message.text or ""
    args_text = raw.partition(" ")[2].strip()

    if not args_text:
        await update.message.reply_text(
            "✂️ Timing do.\n\n"
            "Example:\n"
            "/clip 30 - 50\n"
            "/clip 02:30 - 04:15\n\n"
            "Multiple:\n"
            "/clip\n"
            "00:40 - 01:05\n"
            "03:45 - 04:10"
        )
        return

    try:
        ranges = parse_multiple_ranges(args_text)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    duration = active["duration"]

    for start, end in ranges:
        if end > duration:
            await update.message.reply_text(
                f"❌ Timing video duration se bahar hai.\n\n"
                f"Video duration: {duration:.2f}s\n"
                f"Requested end: {end:.2f}s"
            )
            return

    status = await update.message.reply_text(
        f"⏳ {len(ranges)} clip(s) process ho rahe hain..."
    )

    tracker = ProgressTracker(status)

    try:
        async with job_lock:

            for index, (start, end) in enumerate(ranges, start=1):

                await tracker.status(
                    f"✂️ Clip {index}/{len(ranges)} processing...\n"
                    f"{start:.2f}s → {end:.2f}s"
                )

                output_name = (
                    f"clip_{index}_{int(start)}_{int(end)}.mp4"
                )

                output_path = (
                    user_temp_dir(user_id)
                    / safe_filename(output_name)
                )

                await make_clip(
                    Path(active["path"]),
                    start,
                    end,
                    output_path.name,
                )

                generated = Path(output_path.name)

                if (
                    generated.exists()
                    and generated.resolve() != output_path.resolve()
                ):
                    shutil.move(
                        str(generated),
                        str(output_path),
                    )

                if not output_path.exists():
                    raise FileNotFoundError(
                        f"Clip output not found: {output_path}"
                    )

                await send_output(
                    update,
                    output_path,
                    caption=(
                        f"✂️ Clip {index}/{len(ranges)}\n"
                        f"{start:.2f}s → {end:.2f}s"
                    ),
                )

                output_path.unlink(missing_ok=True)

        await tracker.success("✅ All clips completed!")

    except Exception:
        logger.exception("Clip processing error")

        await tracker.error(
            "❌ Clip processing failed.\n"
            "Technical details Termux log me hain."
        )


async def split_command(update, context):
    user = update.effective_user

    if not user:
        return

    user_id = user.id
    active = active_videos.get(user_id)

    if not active:
        await update.message.reply_text(
            "❌ Pehle ek original video send karo."
        )
        return

    text = update.message.text or ""
    args = text.split(maxsplit=1)

    if len(args) < 2:
        await update.message.reply_text(
            "✂️ Split duration do.\n\n"
            "Examples:\n"
            "/split 30\n"
            "/split 30 12:30\n"
            "/split 30 12:30 - 20:52"
        )
        return

    value = args[1].strip()

    match = re.match(
        r"^(\d+)(?:\s+(.*))?$",
        value,
    )

    if not match:
        await update.message.reply_text(
            "❌ Invalid split format."
        )
        return

    part_duration = int(match.group(1))

    if part_duration <= 0:
        await update.message.reply_text(
            "❌ Split duration positive integer hona chahiye."
        )
        return

    rest = (match.group(2) or "").strip()

    start = 0.0
    end = None

    try:
        if rest:

            if any(x in rest for x in ("-", "–", "—")):

                range_match = re.match(
                    r"^\s*(.+?)\s*[-–—]\s*(.+?)\s*$",
                    rest,
                )

                if not range_match:
                    raise ValueError("Invalid split range.")

                start = parse_time(
                    range_match.group(1).strip()
                )

                end = parse_time(
                    range_match.group(2).strip()
                )

                if end <= start:
                    raise ValueError(
                        "End time must be greater than start."
                    )

            else:
                start = parse_time(rest)

    except Exception as exc:
        await update.message.reply_text(
            f"❌ Invalid timing: {exc}"
        )
        return

    duration = active["duration"]

    if start >= duration:
        await update.message.reply_text(
            "❌ Start time video duration se bahar hai."
        )
        return

    if end is not None and end > duration:
        await update.message.reply_text(
            "❌ End time video duration se bahar hai."
        )
        return

    status = await update.message.reply_text(
        "⏳ Video split ho raha hai..."
    )

    tracker = ProgressTracker(status)

    try:
        async with job_lock:

            await tracker.status(
                "✂️ Splitting ORIGINAL video...\n"
                "Previous split outputs use nahi honge."
            )

            output_dir = (
                user_temp_dir(user_id) / "split"
            )

            output_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            files = await split_video(
                Path(active["path"]),
                part_duration,
                start=start,
                end=end,
            )

        if not files:
            raise RuntimeError(
                "No split files were generated."
            )

        await tracker.status(
            f"📦 {len(files)} part(s) ready.\n"
            "Sending..."
        )

        for index, file_path in enumerate(files, start=1):

            file_path = Path(file_path)

            if not file_path.exists():
                continue

            await send_output(
                update,
                file_path,
                caption=f"✂️ Part {index}/{len(files)}",
            )

            file_path.unlink(missing_ok=True)

        await tracker.success(
            f"✅ Split complete!\n"
            f"{len(files)} part(s) created."
        )

    except Exception:
        logger.exception("Split processing error")

        await tracker.error(
            "❌ Split processing failed.\n"
            "Technical details Termux log me hain."
        )


async def error_handler(update, context):
    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error,
    )



async def save_command(update, context):
    """Owner-only interactive batch source-link saver."""

    user_id = update.effective_user.id

    try:
        require_owner(user_id)
    except PermissionError:
        await update.message.reply_text("❌ Ye command sirf owner use kar sakta hai.")
        return

    text = " ".join(context.args).strip()

    # /save without arguments:
    # resume pending batch if one exists, otherwise start instructions.
    if not text:
        pending = None

        try:
            # Search pending rows from existing DB.
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT batch_id, anime, season, language,
                           COUNT(*) AS total,
                           SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) AS done
                    FROM library
                    WHERE status IN ('pending','processing')
                    GROUP BY batch_id
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

            pending = dict(row) if row else None
        except Exception:
            pending = None

        if pending:
            await update.message.reply_text(
                "🔄 PENDING BATCH MILA\n\n"
                f"🎬 {pending['anime']}\n"
                f"📚 Season {pending['season']}\n"
                f"🌐 {pending['language']}\n"
                f"📊 Complete: {pending['done']}/{pending['total']}\n\n"
                "▶️ Resume kar raha hoon..."
            )

            await process_save_batch(update, pending["batch_id"])
            return

        await update.message.reply_text(
            "💾 SAVE ANIME\n\n"
            "Use:\n"
            "/save Naruto\n\n"
            "Uske baad bot poochega:\n"
            "1️⃣ Content Type\n"
            "2️⃣ Season Number\n"
            "3️⃣ Language\n"
            "4️⃣ Saare source links"
        )
        return

    anime = text

    save_sessions[user_id] = {
        "step": "content_type",
        "anime": anime,
    }

    await update.message.reply_text(
        f"💾 SAVE STARTED\n\n"
        f"🎬 Anime: {anime}\n\n"
        "Content type bhejo:\n\n"
        "1️⃣ Season\n"
        "2️⃣ Movie\n"
        "3️⃣ OVA\n"
        "4️⃣ OAD\n"
        "5️⃣ Special\n\n"
        "Example: Season"
    )


async def save_text_handler(update, context):
    """Handles interactive answers for /save."""

    user_id = update.effective_user.id

    if user_id not in save_sessions:
        return

    try:
        require_owner(user_id)
    except PermissionError:
        save_sessions.pop(user_id, None)
        return

    text = (update.message.text or "").strip()

    if not text:
        return

    session = save_sessions[user_id]
    step = session["step"]

    # ---------------- CONTENT TYPE ----------------
    if step == "content_type":
        value = text.lower()

        types = {
            "1": "season",
            "2": "movie",
            "3": "ova",
            "4": "oad",
            "5": "special",
            "season": "season",
            "movie": "movie",
            "ova": "ova",
            "oad": "oad",
            "special": "special",
        }

        content_type = types.get(value)

        if not content_type:
            await update.message.reply_text(
                "❌ Invalid content type.\n\n"
                "Bhejo: Season / Movie / OVA / OAD / Special"
            )
            return

        session["content_type"] = content_type

        if content_type == "season":
            session["step"] = "season"
            await update.message.reply_text(
                "📚 Season number bhejo.\n\n"
                "Example:\n"
                "3"
            )
        else:
            session["season"] = ""
            session["step"] = "language"

            await update.message.reply_text(
                "🌐 Language bhejo.\n\n"
                "Example:\n"
                "Hindi\n"
                "English\n"
                "Japanese"
            )

        return

    # ---------------- SEASON ----------------
    if step == "season":
        if not text.isdigit():
            await update.message.reply_text(
                "❌ Season number sirf number me bhejo.\n\nExample: 3"
            )
            return

        session["season"] = text
        session["step"] = "language"

        await update.message.reply_text(
            "🌐 Language bhejo.\n\n"
            "Example:\n"
            "Hindi\n"
            "English\n"
            "Japanese"
        )
        return

    # ---------------- LANGUAGE ----------------
    if step == "language":
        session["language"] = text
        session["step"] = "links"

        await update.message.reply_text(
            "🔗 Ab **saare source Telegram links** bhejo.\n\n"
            "Ek link = ek line.\n\n"
            "Example:\n"
            "https://t.me/channel/101\n"
            "https://t.me/channel/102\n"
            "https://t.me/channel/103\n\n"
            "⚠️ Saare links ek message me bhejna.\n"
            "Bot unhe ONE-BY-ONE process karega."
        )
        return

    # ---------------- LINKS ----------------
    if step == "links":
        links = re.findall(r"https?://t\.me/[^\s]+", text, re.IGNORECASE)

        if not links:
            await update.message.reply_text(
                "❌ Koi valid Telegram source link nahi mila."
            )
            return

        valid_links = []

        for link in links:
            link = link.strip().rstrip(".,)")
            try:
                parse_telegram_message_link(link)
                valid_links.append(link)
            except ValueError:
                pass

        if not valid_links:
            await update.message.reply_text(
                "❌ Valid Telegram message links nahi mile."
            )
            return

        batch_id = uuid.uuid4().hex[:16]

        session["links"] = valid_links
        session["batch_id"] = batch_id

        anime = session["anime"]
        season = session.get("season", "")
        language = session["language"]
        content_type = session["content_type"]

        # Create batch rows first.
        try:
            with get_connection() as conn:
                for order, link in enumerate(valid_links, 1):
                    conn.execute(
                        """
                        INSERT INTO library
                        (
                            anime,
                            content_type,
                            season,
                            episode,
                            title,
                            language,
                            source_type,
                            source_url,
                            status,
                            batch_id,
                            source_order,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            anime,
                            "episode" if content_type == "season" else content_type,
                            season,
                            str(order) if content_type == "season" else "",
                            (
                                ""
                                if content_type == "season"
                                else f"{content_type.title()} {order}"
                            ),
                            language,
                            "telegram",
                            link,
                            "pending",
                            batch_id,
                            order,
                            None,
                            None,
                        ),
                    )

                conn.commit()

        except Exception as exc:
            logger.exception("Batch creation failed")
            save_sessions.pop(user_id, None)

            await update.message.reply_text(
                f"❌ Batch create nahi hua.\n\n{exc}"
            )
            return

        save_sessions.pop(user_id, None)

        await update.message.reply_text(
            "📦 BATCH CREATED\n\n"
            f"🎬 {anime}\n"
            f"📚 {('Season ' + season) if season else content_type.title()}\n"
            f"🌐 {language}\n"
            f"🔗 Links: {len(valid_links)}\n\n"
            "⚙️ One-by-one processing start..."
        )

        await process_save_batch(update, batch_id)


async def process_save_batch(update, batch_id):
    """Process one source at a time and permanently keep only index + DB metadata."""

    global telethon_client

    user_id = update.effective_user.id

    try:
        batch = get_batch(batch_id)

        if not batch:
            await update.message.reply_text("❌ Batch nahi mila.")
            return

        total = len(batch)

        for position, item in enumerate(batch, 1):

            # Skip already completed entries.
            if item["status"] == "ready":
                continue

            item_id = item["id"]
            source_url = item["source_url"]

            anime = item["anime"]
            season = item["season"]
            episode = item["episode"]

            status_msg = await update.message.reply_text(
                "⚙️ PROCESSING\n\n"
                f"🎬 {anime}\n"
                f"📚 {('S' + season + ' ') if season else ''}"
                f"{('E' + episode) if episode else item['title']}\n\n"
                f"📊 {position}/{total}\n"
                "⬇️ Source download..."
            )

            temp_source = None

            try:
                update_library_item(
                    item_id,
                    status="processing",
                    error_message="",
                    last_attempt_at="datetime('now')",
                )

                chat, message_id = parse_telegram_message_link(source_url)

                if telethon_client is None:
                    raise RuntimeError(
                        "Telegram source client connected nahi hai."
                    )

                temp_source = await download_telethon_message(
                    telethon_client,
                    chat,
                    message_id,
                    user_id,
                )

                temp_source = Path(temp_source)

                if not temp_source.exists():
                    raise RuntimeError("Source download file nahi mila.")

                await status_msg.edit_text(
                    "⚙️ PROCESSING\n\n"
                    f"🎬 {anime}\n"
                    f"📊 {position}/{total}\n\n"
                    "🔎 Building compact visual fingerprint..."
                )

                index_file = index_path_for(
                    anime,
                    season or "0",
                    episode or str(position),
                )

                duration_value = await build_index_async(
                    temp_source,
                    index_file,
                )

                if isinstance(duration_value, (int, float)):
                    duration_value = float(duration_value)
                else:
                    duration_value = 0.0

                update_library_item(
                    item_id,
                    index_path=str(index_file),
                    duration=duration_value,
                    status="ready",
                    error_message="",
                )

                # IMPORTANT:
                # Source video is deleted immediately after index creation.
                try:
                    temp_source.unlink(missing_ok=True)
                except Exception:
                    pass

                await status_msg.edit_text(
                    "✅ SAVED\n\n"
                    f"🎬 {anime}\n"
                    f"📊 {position}/{total}\n"
                    "🔎 Fingerprint: READY\n"
                    "💾 Source video: DELETED\n"
                    "🔗 Source link: SAVED"
                )

            except Exception as exc:
                logger.exception(
                    "Batch item failed: %s / %s",
                    batch_id,
                    item_id,
                )

                update_library_item(
                    item_id,
                    status="pending",
                    error_message=str(exc),
                )

                if temp_source:
                    try:
                        Path(temp_source).unlink(missing_ok=True)
                    except Exception:
                        pass

                await status_msg.edit_text(
                    "⚠️ EPISODE PENDING\n\n"
                    f"🎬 {anime}\n"
                    f"📊 {position}/{total}\n\n"
                    f"❌ {exc}\n\n"
                    "Already completed episodes SAFE hain.\n"
                    "Network/source available hone par /save se resume kar sakte ho."
                )

                # Network/source failure:
                # stop here, don't process remaining links.
                break

        # Final status
        remaining = get_pending_batch(batch_id)

        if not remaining:
            await update.message.reply_text(
                "🎉 BATCH COMPLETE!\n\n"
                f"🎬 {batch[0]['anime']}\n"
                f"📊 {total}/{total} processed\n\n"
                "💾 Videos permanently store nahi kiye gaye.\n"
                "🔎 Compact fingerprints + source links saved hain."
            )
        else:
            await update.message.reply_text(
                "⏸️ BATCH PAUSED\n\n"
                f"✅ Completed: {total - len(remaining)}/{total}\n"
                f"⏳ Pending: {len(remaining)}\n\n"
                "Network/source issue solve hone ke baad:\n"
                "/save\n\n"
                "Bot completed episodes repeat nahi karega."
            )

    except Exception:
        logger.exception("Batch processing error")
        await update.message.reply_text(
            "❌ Batch processing error.\n"
            "Completed entries DB me safe hain."
        )

async def findclip_command(update, context):
    user_id = update.effective_user.id
    reply = update.message.reply_to_message

    if not reply or not getattr(reply, "video", None):
        await update.message.reply_text(
            "🔎 FINDCLIP\n\n"
            "Edited video ko reply karke:\n"
            "/findclip\n\n"
            "Bot saved episodes ko scan karega."
        )
        return

    status = await update.message.reply_text(
        "🔎 FINDCLIP PROCESSING\n\n"
        "⬇️ Edited video download ho raha hai..."
    )

    query_path = None

    try:
        query_path = await download_bot_video(reply, user_id)

        index_files = list(INDEX_DIR.glob("*.json"))

        if not index_files:
            await status.edit_text(
                "❌ Koi saved/indexed episode nahi mila.\n\n"
                "Pehle source video save karo:\n"
                "/save Naruto S1 E1"
            )
            return

        total = len(index_files)
        matches = []

        for number, index_file in enumerate(index_files, 1):
            percent = int(number * 100 / total)

            await status.edit_text(
                "🔎 FINDCLIP PROCESSING\n\n"
                f"📊 Overall: {percent}%\n"
                f"🎞 Episodes scanned: {number}/{total}\n"
                f"🎯 Matches: {len(matches)}"
            )

            try:
                data = load_index(index_file)

                results = await search_index_async(
                    query_path,
                    data,
                )

                result = continuous_match(
                    results,
                    data.get("duration", 0),
                )

                if result:
                    matches.append({
                        "index": index_file,
                        "data": data,
                        "match": result,
                    })

            except Exception:
                logger.exception(
                    "Index scan failed: %s",
                    index_file,
                )

        if not matches:
            await status.edit_text(
                "❌ SCAN COMPLETE\n\n"
                f"🎞 Episodes scanned: {total}\n"
                "🎯 Confirmed matches: 0"
            )
            return

        best = max(
            matches,
            key=lambda x: x["match"]["confidence"],
        )

        data = best["data"]
        match = best["match"]
        # Source video is NOT permanently stored.
        # Resolve source URL from library DB and download temporarily.
        matched_anime = data.get("anime", "")
        matched_season = data.get("season", "")
        matched_episode = data.get("episode", "")

        source = None
        library_row = None

        try:
            with get_connection() as conn:
                library_row = conn.execute(
                    """
                    SELECT *
                    FROM library
                    WHERE LOWER(anime) = LOWER(?)
                    AND season = ?
                    AND episode = ?
                    AND content_type = 'episode'
                    AND status = 'ready'
                    LIMIT 1
                    """,
                    (
                        matched_anime,
                        str(matched_season),
                        str(matched_episode),
                    ),
                ).fetchone()
        except Exception:
            library_row = None

        if not library_row:
            # Try index filename metadata if available.
            try:
                index_name = best["index"].stem
                with get_connection() as conn:
                    library_row = conn.execute(
                        """
                        SELECT *
                        FROM library
                        WHERE index_path = ?
                        AND status = 'ready'
                        LIMIT 1
                        """,
                        (str(best["index"]),),
                    ).fetchone()
            except Exception:
                library_row = None

        if not library_row:
            await status.edit_text(
                "❌ Match mila, lekin saved source link nahi mila."
            )
            return

        source_url = library_row["source_url"]

        if not source_url:
            await status.edit_text(
                "❌ Is episode ka source link missing hai."
            )
            return

        chat, message_id = parse_telegram_message_link(source_url)

        if telethon_client is None:
            await status.edit_text(
                "❌ Telegram source client connected nahi hai."
            )
            return

        await status.edit_text(
            "🎯 MATCH FOUND!\n\n"
            f"📊 Confidence: {match['confidence']}%\n"
            f"⏱ {match['source_start']:.1f}s – "
            f"{match['source_end']:.1f}s\n\n"
            "⬇️ Downloading source temporarily..."
        )

        source = await download_telethon_message(
            telethon_client,
            chat,
            message_id,
            user_id,
        )
        source = Path(source)

        await status.edit_text(
            "🎯 MATCH FOUND!\n\n"
            f"📊 Confidence: {match['confidence']}%\n"
            f"⏱ {match['source_start']:.1f}s – "
            f"{match['source_end']:.1f}s\n\n"
            "✂️ Creating source clip..."
        )

        output = TEMP_DIR / (
            f"findclip_{user_id}.mp4"
        )

        ok = await create_clip_async(
            source,
            match["source_start"],
            match["source_end"] + 1.0,
            output,
        )

        if not ok:
            await status.edit_text(
                "❌ Source clip create nahi ho paya."
            )
            return

        await send_output(
            update,
            output,
            caption=(
                "🎯 FindClip Match\n"
                f"⏱ {match['source_start']:.1f}s – "
                f"{match['source_end']:.1f}s\n"
                f"📊 Confidence: {match['confidence']}%"
            ),
        )

        output.unlink(missing_ok=True)

        await status.edit_text(
            "✅ FINDCLIP COMPLETE\n\n"
            "🎯 Matching source clip mil gaya."
        )

    except Exception:
        logger.exception("FindClip error")
        await status.edit_text(
            "❌ FindClip processing failed.\n"
            "Termux log me technical details hain."
        )

    finally:
        if query_path:
            try:
                Path(query_path).unlink(missing_ok=True)
            except Exception:
                pass

        if "source" in locals() and source:
            try:
                Path(source).unlink(missing_ok=True)
            except Exception:
                pass

async def telegram_client_start(application):
    global telethon_client

    if not TG_API_ID or not TG_API_HASH:
        logger.warning(
            "TG_API_ID/TG_API_HASH missing. Source-link saving disabled."
        )
        return

    telethon_client = TelegramClient(
        TELEGRAM_SESSION,
        TG_API_ID,
        TG_API_HASH,
    )

    await telethon_client.start()
    logger.info("Telethon source client connected.")


async def telegram_client_stop(application):
    global telethon_client

    if telethon_client:
        await telethon_client.disconnect()
        telethon_client = None
        logger.info("Telethon source client disconnected.")


def main():
    validate_bot_config()

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN missing. Environment variable set karo."
        )

    Path(TEMP_DIR).mkdir(
        parents=True,
        exist_ok=True,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("next", next_command)
    )

    application.add_handler(
        CommandHandler("clip", clip_command)
    )

    application.add_handler(
        CommandHandler("split", split_command)
    )
    application.add_handler(
        CommandHandler("findclip", findclip_command)
    )
    application.add_handler(
        CommandHandler("save", save_command)
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            receive_video,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            save_text_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("========================================")
    logger.info("Telegram Video Bot starting...")
    logger.info("========================================")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
