import base64
import logging
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import get_connection

logger = logging.getLogger("anime-bot.library")


# Canonical titles for the anime the source group is intended to contain.
# Matching is deliberately conservative; unrelated topic names are not added
# merely because they contain a word like "anime" or "clip".
_CANONICAL = {
    "a gentle noble's vacation recommendation": "A Gentle Noble's Vacation Recommendation",
    "odayaka kizoku no kyuuka no susume": "A Gentle Noble's Vacation Recommendation",
    "agents of the four seasons dance of spring": "Agents of the Four Seasons: Dance of Spring",
    "a gatherer's adventure in isekai": "A Gatherer's Adventure in Isekai",
    "akame ga kill": "Akame Ga Kill",
    "attack on titan": "Attack on Titan",
    "death note": "Death Note",
    "an adventure daily grind at age 29": "An Adventure Daily Grind at Age 29",
    "naruto": "Naruto",
    "naruto shippuden": "Naruto Shippuden",
    "naruto movies": "Naruto Movies",
}

_TOPIC_EXCLUDES = {
    "chats",
    "anime panel",
    "welcome",
    "pfp and wallpaper",
    "pfp & wallpapers",
    "twixter",
    "clips",
    "feedback",
    "application",
    "cc",
    "ai videos",
    "overlay",
    "phonks",
}


def _norm(value: str) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"[._|•·]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_:[]()")
    return value


def canonical_anime(value: str):
    raw = (value or "").strip()
    norm = _norm(raw)
    if not norm or norm in _TOPIC_EXCLUDES:
        return None

    if norm in _CANONICAL:
        return _CANONICAL[norm]

    # Safe variants seen in the existing source scan.
    if norm.replace(" ", "") == "attackontitan":
        return "Attack on Titan"
    if norm.startswith("ndiaattack on titan") or norm.endswith("attack on titan"):
        return "Attack on Titan"
    if norm.startswith("naruto shippuden"):
        return "Naruto Shippuden"
    if norm.startswith("naruto movies"):
        return "Naruto Movies"
    if norm == "naruto":
        return "Naruto"

    # Do not invent new anime names from arbitrary forum topics.
    return None


def _token(value: str) -> str:
    data = value.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _untoken(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def _load_tree():
    tree = {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT anime, season, episode, quality, source_url
            FROM library
            ORDER BY anime COLLATE NOCASE, season, CAST(episode AS INTEGER)
            """
        ).fetchall()

    for row in rows:
        anime = canonical_anime(row["anime"])
        if not anime:
            continue
        season = str(row["season"])
        episode = str(row["episode"])
        tree.setdefault(anime, {}).setdefault(season, {}).setdefault(episode, {})[
            row["quality"]
        ] = row["source_url"]
    return tree


def _season_sort_key(value: str):
    match = re.search(r"\d+", str(value))
    if match:
        return (0, int(match.group()))
    labels = {"ova": 1, "oad": 2, "special": 3, "movie": 4}
    return (labels.get(_norm(value), 9), _norm(value))


def _quality_links(sources):
    preferred = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "auto"]
    links = []
    for quality in preferred:
        url = sources.get(quality)
        if url:
            label = "Source" if quality == "auto" else quality
            links.append(f'<a href="{escape(url, quote=True)}">{label}</a>')
    return " · ".join(links)


def _keyboard(rows):
    return InlineKeyboardMarkup(rows)


def _anime_page(tree):
    rows = []
    names = sorted(tree, key=str.casefold)
    for anime in names:
        data = _token(anime)
        if len(data) <= 55:
            rows.append([InlineKeyboardButton(f"🎬 {anime}", callback_data=f"la:{data}")])
    return "📚 <b>ANIME LIBRARY</b>\n\nTap an anime:", _keyboard(rows)


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


def _season_page(tree, anime):
    seasons = tree.get(anime, {})
    rows = []
    for season in sorted(seasons, key=_season_sort_key):
        payload = f"{anime}|{season}"
        data = _token(payload)
        if len(data) <= 55:
            rows.append([InlineKeyboardButton(f"📺 {_content_label(season)}", callback_data=f"ls:{data}")])
    rows.append([InlineKeyboardButton("⬅️ Anime", callback_data="lb")])
    return f"🎬 <b>{escape(anime)}</b>\n\nChoose Season / OVA / Movie:", _keyboard(rows)


def _episode_page(tree, anime, season):
    episodes = tree.get(anime, {}).get(season, {})
    lines = [f"🎬 <b>{escape(anime)}</b>", f"📺 <b>{escape(_content_label(season))}</b>", ""]
    rows = []

    for episode in sorted(episodes, key=lambda x: int(x) if str(x).isdigit() else str(x)):
        sources = episodes[episode]
        links = _quality_links(sources)
        lines.append(f"🎞️ <b>Episode {escape(episode)}</b> — {links}")

    rows.append([InlineKeyboardButton("⬅️ Seasons", callback_data=f"la:{_token(anime)}")])
    return "\n".join(lines), _keyboard(rows)


async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tree = _load_tree()
    if not tree:
        await update.message.reply_text("📚 Library abhi empty hai.")
        return
    text, markup = _anime_page(tree)
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def library_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tree = _load_tree()
    data = query.data or ""

    try:
        if data == "lb":
            text, markup = _anime_page(tree)
        elif data.startswith("la:"):
            anime = _untoken(data[3:])
            if anime not in tree:
                await query.answer("Anime library entry nahi mila.", show_alert=True)
                return
            text, markup = _season_page(tree, anime)
        elif data.startswith("ls:"):
            anime, season = _untoken(data[3:]).split("|", 1)
            if anime not in tree or season not in tree[anime]:
                await query.answer("Season library entry nahi mila.", show_alert=True)
                return
            text, markup = _episode_page(tree, anime, season)
        else:
            return

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Library navigation failed")
        await query.answer("Library load failed.", show_alert=True)
