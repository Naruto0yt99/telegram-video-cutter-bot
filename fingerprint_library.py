import base64
import html
import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import FINGERPRINT_CHAT

logger = logging.getLogger("anime-bot.saves")

BOT_USERNAME = "AnimeclipcutterBot"
TOPIC_EXCLUDES = {"raw fingerprint", "fingerprint pdf", "visual index", "visual index pdf"}

def _enc(route):
    return base64.urlsafe_b64encode(route.encode()).decode().rstrip("=")

def _dec(value):
    raw = value[5:] if value.startswith("save_") else value
    raw += "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw.encode()).decode()

def _link(bot_username, label, route):
    return f'<a href="https://t.me/{bot_username}?start=save_{_enc(route)}">{html.escape(label)}</a>'

def _msg_link(label, message):
    username = str(FINGERPRINT_CHAT).lstrip("@")
    message_id = int(getattr(message, "id", 0) or 0)
    return f'<a href="https://t.me/{username}/{message_id}">{html.escape(label)}</a>'

def _caption(message):
    text = str(getattr(message, "message", "") or "")
    return text

def _artifact_kind(message):
    text = _caption(message).casefold()
    file_obj = getattr(message, "file", None)
    name = str(getattr(file_obj, "name", "") or "").casefold()
    if "raw fingerprint" in text or name.endswith(".json"):
        return "json"
    if "fingerprint pdf" in text or name.endswith(".pdf"):
        return "pdf"
    if "visual index" in text or "visual index pdf" in text or name.endswith((".jpg", ".jpeg", ".png", ".pdf")):
        return "index"
    return None

def _episode_key_from_message(message):
    text = " ".join(
        x for x in [
            _caption(message),
            str(getattr(getattr(message, "file", None), "name", "") or ""),
        ] if x
    )
    match = re.search(
        r"(?:^|\n|📚\s*)(?P<anime>.+?)\s+S(?P<season>\d+)\s+E(?P<episode>\d+)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    anime = re.sub(r"^(?:🧠\s*|📄\s*|🖼️\s*|📚\s*)", "", match.group("anime")).strip()
    anime = re.sub(r"\s+(?:raw fingerprint|fingerprint pdf|visual index).*?$", "", anime, flags=re.IGNORECASE)
    return anime.strip(), int(match.group("season")), int(match.group("episode"))

async def _load_artifacts(context):
    client = context.application.bot_data.get("telethon_client")
    if client is None:
        # bot.py stores the live client globally; fallback is injected there.
        return {}
    topic_id = None
    try:
        from fingerprint_storage import get_fingerprint_topic_id
        topic_id = get_fingerprint_topic_id()
    except Exception:
        pass
    if topic_id is None:
        return {}

    artifacts = {}
    async for message in client.iter_messages(FINGERPRINT_CHAT, limit=5000):
        if getattr(message, "media", None) is None:
            continue

        # Keep only artifacts from the bound FINGERPRINTS forum topic.
        reply_to = getattr(message, "reply_to", None)
        top_id = getattr(reply_to, "reply_to_top_id", None) if reply_to else None
        reply_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
        legacy_reply = getattr(message, "reply_to_msg_id", None)
        if topic_id not in {top_id, reply_msg_id, legacy_reply}:
            continue

        kind = _artifact_kind(message)
        key = _episode_key_from_message(message)
        if not kind or not key:
            continue
        artifacts.setdefault(key, {})[kind] = message
    return artifacts

def _render(artifacts, route):
    parts = route.split("|")
    if parts == ["home"]:
        lines = ["🧠 <b>SAVED FINGERPRINTS</b>", ""]
        animes = sorted({k[0] for k in artifacts}, key=str.casefold)
        lines += [f"🎬 {_link(BOT_USERNAME, a, 'a|' + a)}" for a in animes]
        return "\n".join(lines)

    if len(parts) == 2 and parts[0] == "a":
        anime = parts[1]
        seasons = sorted({k[1] for k in artifacts if k[0] == anime})
        lines = [f"🎬 <b>{html.escape(anime)}</b>", ""]
        for season in seasons:
            lines.append(f"📁 {_link(BOT_USERNAME, 'Season ' + str(season), 's|' + anime + '|' + str(season))}")
        lines.append("")
        lines.append(f"⬅️ {_link(BOT_USERNAME, 'Back', 'home')}")
        return "\n".join(lines)

    if len(parts) == 3 and parts[0] == "s":
        anime, season = parts[1], int(parts[2])
        episodes = sorted({k[2] for k in artifacts if k[0] == anime and k[1] == season})
        lines = [f"🎬 <b>{html.escape(anime)}</b>", f"📁 <b>Season {season}</b>", ""]
        for episode in episodes:
            lines.append(f"🎞️ {_link(BOT_USERNAME, 'Episode ' + str(episode), 'e|' + anime + '|' + str(season) + '|' + str(episode))}")
        lines.append("")
        lines.append(f"⬅️ {_link(BOT_USERNAME, 'Back', 'a|' + anime)}")
        return "\n".join(lines)

    if len(parts) == 4 and parts[0] == "e":
        anime, season, episode = parts[1], int(parts[2]), int(parts[3])
        item = artifacts.get((anime, season, episode), {})
        lines = [
            f"🎬 <b>{html.escape(anime)}</b>",
            f"📁 <b>Season {season}</b>",
            f"🎞️ <b>Episode {episode}</b>",
            "",
        ]
        if item.get("pdf"):
            lines.append("📄 " + _msg_link("Fingerprint PDF", item["pdf"]))
        if item.get("json"):
            lines.append("🧾 " + _msg_link("Raw Fingerprint JSON", item["json"]))
        if item.get("index"):
            lines.append("🗂️ " + _msg_link("Visual Index PDF", item["index"]))
        if len(lines) == 4:
            lines.append("❌ Artifacts missing.")
        lines.append("")
        lines.append(f"⬅️ {_link(BOT_USERNAME, 'Back', 's|' + anime + '|' + str(season))}")
        return "\n".join(lines)

    return _render(artifacts, "home")

async def saves_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None:
        return
    artifacts = await _load_artifacts(context)
    if not artifacts:
        await update.message.reply_text(
            "🧠 Saved fingerprints abhi nahi mile.\n"
            "Pehle /fingerprint Death Note S1 E1 chalao."
        )
        return

    route = "home"
    raw = " ".join(context.args or []).strip()
    direct = re.match(
        r"^(.+?)\s+(?:[Ss]eason\s*)?(\d+)\s+[Ee](?:p(?:isode)?\s*)?(\d+)$",
        raw,
        re.IGNORECASE,
    )
    if direct:
        anime, season, episode = direct.groups()
        route = f"e|{anime.strip()}|{int(season)}|{int(episode)}"

    text = _render(artifacts, route)
    sent = await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    context.application.bot_data.setdefault("saves_messages", {})[update.effective_user.id] = (sent.chat_id, sent.message_id)

async def saves_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].startswith("save_"):
        return False
    try:
        artifacts = await _load_artifacts(context)
        route = _dec(args[0])
        text = _render(artifacts, route)
        target = context.application.bot_data.get("saves_messages", {}).get(update.effective_user.id)
        if target:
            await context.bot.edit_message_text(
                chat_id=target[0],
                message_id=target[1],
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        if update.message:
            try:
                await update.message.delete()
            except Exception:
                pass
        return True
    except Exception:
        logger.exception("Saved fingerprint navigation failed")
        return False
