from pathlib import Path

p = Path("bot.py")
s = p.read_text()

# ---------- imports ----------
s = s.replace(
    "from telegram_media import download_bot_video",
    """from telegram_media import (
    download_bot_video,
    parse_telegram_message_link,
    download_telethon_message,
)
from permissions import require_owner
from database import update_library_item, get_batch, get_pending_batch
from library import save_episode"""
)

s = s.replace(
    "from pathlib import Path",
    """from pathlib import Path
import uuid
from telethon import TelegramClient
from config import TG_API_ID, TG_API_HASH, TELEGRAM_SESSION"""
)

# ---------- globals ----------
marker = 'job_lock = asyncio.Lock()'
replacement = '''job_lock = asyncio.Lock()

# Interactive /save session state
save_sessions = {}

# Telegram user client used only for fetching source links temporarily
telethon_client = None
'''
s = s.replace(marker, replacement, 1)

# ---------- replace old save_command ----------
start = s.index("async def save_command(update, context):")
end = s.index("\nasync def findclip_command", start)

new_save = r'''async def save_command(update, context):
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
                            source_order
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
'''

s = s[:start] + new_save + s[end:]

# ---------- replace findclip source resolution ----------
old = '''source = Path(data["source"])

        if not source.exists():
            await status.edit_text(
                "❌ Match mila, lekin source video nahi mila."
            )
            return'''

new = '''# Source video is NOT permanently stored.
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
            "🎯 MATCH FOUND!\\n\\n"
            f"📊 Confidence: {match['confidence']}%\\n"
            f"⏱ {match['source_start']:.1f}s – "
            f"{match['source_end']:.1f}s\\n\\n"
            "⬇️ Downloading source temporarily..."
        )

        source = await download_telethon_message(
            telethon_client,
            chat,
            message_id,
            user_id,
        )
        source = Path(source)'''

if old not in s:
    print("WARNING: old findclip source block not found")
else:
    s = s.replace(old, new, 1)

# ---------- ensure findclip deletes temporary source ----------
old_finally = '''finally:
        if query_path:
            try:
                Path(query_path).unlink(missing_ok=True)
            except Exception:
                pass'''

new_finally = '''finally:
        if query_path:
            try:
                Path(query_path).unlink(missing_ok=True)
            except Exception:
                pass

        if "source" in locals() and source:
            try:
                Path(source).unlink(missing_ok=True)
            except Exception:
                pass'''

s = s.replace(old_finally, new_finally, 1)

# ---------- add get_connection import ----------
s = s.replace(
    "from database import update_library_item, get_batch, get_pending_batch",
    "from database import update_library_item, get_batch, get_pending_batch, get_connection"
)

# ---------- add Telethon lifecycle ----------
main_marker = '''def main():
    validate_bot_config()'''

main_replacement = '''async def telegram_client_start(application):
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
    validate_bot_config()'''

s = s.replace(main_marker, main_replacement, 1)

# ---------- add application lifecycle ----------
s = s.replace(
    ''').build()
    )

    application.add_handler(''',
    ''').build()

    application.post_init = telegram_client_start
    application.post_shutdown = telegram_client_stop

    application.add_handler(''',
    1
)

# ---------- add interactive text handler ----------
target = '''application.add_handler(
        MessageHandler(
            filters.VIDEO,
            receive_video,
        )
    )'''

replacement_handler = '''application.add_handler(
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
    )'''

if target not in s:
    print("WARNING: video handler block not found")
else:
    s = s.replace(target, replacement_handler, 1)

p.write_text(s)
print("SAVE integration patch applied.")
