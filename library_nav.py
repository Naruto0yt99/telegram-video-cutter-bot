import base64
import html
import logging
import re
import unicodedata

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import get_connection

logger = logging.getLogger("anime-bot.library")

BOT_USERNAME = "AnimeclipcutterBot"

_CANONICAL = {
    "a gentle noble's vacation recommendation": "A Gentle Noble's Vacation Recommendation",
    "odayaka kizoku no kyuuka no susume": "A Gentle Noble's Vacation Recommendation",
    "agents of the four seasons dance of spring": "Agents of the Four Seasons: Dance of Spring",
    "a gatherer's adventure in isekai": "A Gatherer's Adventure in Isekai",
    "akame ga kill": "Akame Ga Kill",
    "attack on titan": "Attack on Titan",
    "aot": "Attack on Titan",
    "death note": "Death Note",
    "dn": "Death Note",
    "an adventure daily grind at age 29": "An Adventure Daily Grind at Age 29",
    "naruto": "Naruto",
    "naruto shippuden": "Naruto Shippuden",
    "naruto movies": "Naruto Movies",
    "re zero": "Re:Zero",
    "re:zero": "Re:Zero",
    "re zero starting life": "Re:Zero",
    "re zero starting life i": "Re:Zero",
    "re zero starting life in": "Re:Zero",
    "re zero starting life in another world": "Re:Zero",
    "re:zero starting life": "Re:Zero",
    "re:zero starting life i": "Re:Zero",
    "re:zero starting life in": "Re:Zero",
    "re:zero starting life in another world": "Re:Zero",
}

_TOPIC_EXCLUDES = {
    "chats", "anime panel", "welcome", "pfp and wallpaper", "pfp & wallpapers",
    "twixter", "clips", "clip cutter", "feedback", "application", "cc",
    "ai videos", "overlay", "phonks", "normal video", "normal videos",
    "random", "other", "others", "misc", "miscellaneous",
}

def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"_+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _norm(value))

def canonical_anime(value: str):
    norm = _norm(value)
    if not norm or norm in _TOPIC_EXCLUDES:
        return None
    if norm in _CANONICAL:
        return _CANONICAL[norm]
    compact = _compact(value)
    for key, canonical in _CANONICAL.items():
        if _compact(key) == compact:
            return canonical
    for key in sorted(_CANONICAL, key=len, reverse=True):
        if _norm(key) in norm:
            return _CANONICAL[key]
    return re.sub(r"\s+", " ", value or "").strip() or None

def _encode_route(route: str) -> str:
    return base64.urlsafe_b64encode(route.encode("utf-8")).decode("ascii").rstrip("=")

def _decode_route(payload: str) -> str:
    raw = payload[4:] if payload.startswith("lib_") else payload
    raw += "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")

def _link(bot_username: str, label: str, route: str) -> str:
    return f'<a href="https://t.me/{bot_username}?start=lib_{_encode_route(route)}">{html.escape(label)}</a>'

def _source_link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'

def _load_tree():
    tree = {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT anime, series, season, episode, quality, source_url
            FROM library
            ORDER BY anime COLLATE NOCASE, series COLLATE NOCASE,
                     CAST(season AS INTEGER), CAST(episode AS INTEGER)
            """
        ).fetchall()

    for row in rows:
        anime = canonical_anime(row["anime"])
        if not anime:
            continue
        series = canonical_anime(row["series"] or row["anime"]) or str(row["series"] or row["anime"])
        # Keep OG Naruto, Shippuden and Movies as separate series under one Naruto node.
        if series not in {"Naruto", "Naruto Shippuden", "Naruto Movies"} and anime == "Naruto":
            series = "Naruto"
        season = str(row["season"])
        episode = str(row["episode"])
        tree.setdefault(anime, {}).setdefault(series, {}).setdefault(season, {}).setdefault(episode, {})[
            row["quality"]
        ] = row["source_url"]
    return tree

def _season_sort_key(value: str):
    match = re.search(r"\d+", str(value))
    if match:
        return (0, int(match.group()))
    labels = {"ova": 1, "oad": 2, "special": 3, "movie": 4}
    return (labels.get(_norm(value), 9), _norm(value))

def _content_label(season):
    norm = _norm(season)
    if norm == "ova":
        return "OVA"
    if norm == "oad":
        return "OAD"
    if norm == "special":
        return "Specials"
    if norm == "movie":
        return "Movies"
    return f"Season {season}"

def _anime_page(tree, bot_username):
    lines = ["📚 <b>ANIME LIBRARY</b>", "", ""] 
    lines.extend(f"🎬 {_link(bot_username, anime, 'a|' + anime)}" for anime in sorted(tree, key=str.casefold))
    return "\n".join(lines), None

def _series_page(tree, anime, bot_username):
    series = tree.get(anime, {})
    lines = [f"🎬 <b>{html.escape(anime)}</b>", ""]
    for name in sorted(series, key=str.casefold):
        lines.append(f"📺 {_link(bot_username, name, 's|' + anime + '|' + name)}")
    lines.extend(["", f"⬅️ {_link(bot_username, 'Back', 'home')}"])
    return "\n".join(lines), None

def _season_page(tree, anime, series, bot_username):
    seasons = tree.get(anime, {}).get(series, {})
    lines = [
        f"🎬 <b>{html.escape(anime)}</b>",
        f"📺 <b>{html.escape(series)}</b>",
        "",
    ]
    for season in sorted(seasons, key=_season_sort_key):
        lines.append(
            f"📁 {_link(bot_username, _content_label(season), 't|' + anime + '|' + series + '|' + season)}"
        )
    lines.extend(["", f"⬅️ {_link(bot_username, 'Back', 'a|' + anime)}"])
    return "\n".join(lines), None

def _episode_page(tree, anime, series, season, bot_username):
    episodes = tree.get(anime, {}).get(series, {}).get(season, {})
    lines = [
        f"🎬 <b>{html.escape(anime)}</b>",
        f"📺 <b>{html.escape(series)}</b>",
        f"📁 <b>{html.escape(_content_label(season))}</b>",
        "",
    ]
    preferred = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "auto"]
    for episode in sorted(episodes, key=lambda x: int(x) if str(x).isdigit() else str(x)):
        sources = episodes[episode]
        links = []
        for quality in preferred:
            url = sources.get(quality)
            if url:
                links.append(_source_link("Source" if quality == "auto" else quality, url))
        if links:
            lines.append(f"🎞️ <b>Episode {html.escape(episode)}</b> — " + " / ".join(links))
    if len(lines) == 4:
        lines.append("No episodes found.")
    lines.extend(["", f"⬅️ {_link(bot_username, 'Back', 's|' + anime + '|' + series)}"])
    return "\n".join(lines), None

def _render_route(tree, route, bot_username):
    parts = route.split("|")
    if parts == ["home"]:
        return _anime_page(tree, bot_username)
    if parts and parts[0] == "a" and len(parts) == 2:
        anime = parts[1]
        return _series_page(tree, anime, bot_username)
    if parts and parts[0] == "s" and len(parts) == 3:
        return _season_page(tree, parts[1], parts[2], bot_username)
    if parts and parts[0] == "t" and len(parts) == 4:
        return _episode_page(tree, parts[1], parts[2], parts[3], bot_username)
    return _anime_page(tree, bot_username)

async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tree = _load_tree()
    if not tree:
        await update.message.reply_text("📚 Library abhi empty hai.")
        return
    bot_username = context.bot.username or BOT_USERNAME
    text, _ = _anime_page(tree, bot_username)
    sent = await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    context.application.bot_data.setdefault("library_messages", {})[update.effective_user.id] = (
        sent.chat_id,
        sent.message_id,
    )

async def library_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].startswith("lib_"):
        return False

    try:
        route = _decode_route(args[0])
        tree = _load_tree()
        bot_username = context.bot.username or BOT_USERNAME
        text, _ = _render_route(tree, route, bot_username)
        target = context.application.bot_data.get("library_messages", {}).get(update.effective_user.id)

        if target:
            chat_id, message_id = target
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            context.application.bot_data["library_messages"][update.effective_user.id] = (chat_id, message_id)

        # The deep-link /start message itself is only a navigation trigger.
        if update.message:
            try:
                await update.message.delete()
            except Exception:
                pass
        return True
    except Exception:
        logger.exception("Library deep-link navigation failed")
        return False
