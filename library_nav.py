import base64
import hashlib
import logging
import re
import unicodedata

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import get_connection

logger = logging.getLogger("anime-bot.library")

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
    "re zero": "Re:Zero",
    "re:zero": "Re:Zero",
    "re zero starting life": "Re:Zero",
    "re zero starting life in another world": "Re:Zero",
    "re:zero starting life": "Re:Zero",
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
    value = re.sub(r"\s+", " ", value).strip()
    return value

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
        if key in norm:
            return _CANONICAL[key]
    return re.sub(r"\s+", " ", value or "").strip() or None

def _callback_key(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]

def _keyboard(rows):
    return InlineKeyboardMarkup(rows)

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
        # All Naruto variants intentionally live below one public Naruto node.
        if series in {"Naruto Shippuden", "Naruto Movies"}:
            anime = "Naruto"
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

def _merged_series_seasons(tree, anime, series):
    return tree.get(anime, {}).get(series, {})

def _anime_page(tree):
    rows = [
        [InlineKeyboardButton(f"🎬 {anime}", callback_data=f"la:{_callback_key(anime)}")]
        for anime in sorted(tree, key=str.casefold)
    ]
    return "📚 <b>ANIME LIBRARY</b>\n\nTap an anime:", _keyboard(rows)

def _series_page(tree, anime):
    series = tree.get(anime, {})
    # If there is only one internal series, skip an unnecessary extra screen.
    if len(series) == 1:
        only = next(iter(series))
        return _season_page(tree, anime, only)

    rows = []
    for name in sorted(series, key=str.casefold):
        rows.append([
            InlineKeyboardButton(
                f"📺 {name}",
                callback_data=f"ls:{_callback_key(anime + '|' + name)}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="lb")])
    return f"🎬 <b>{anime}</b>\n\nSelect series:", _keyboard(rows)

def _season_page(tree, anime, series):
    seasons = _merged_series_seasons(tree, anime, series)
    rows = []
    for season in sorted(seasons, key=_season_sort_key):
        rows.append([
            InlineKeyboardButton(
                f"📺 {_content_label(season)}",
                callback_data=f"lt:{_callback_key(anime + '|' + series + '|' + season)}",
            )
        ])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data=f"la:{_callback_key(anime)}")
    ])
    return (
        f"🎬 <b>{anime}</b>\n"
        f"📺 <b>{series}</b>\n\n"
        "Select season:",
        _keyboard(rows),
    )

def _episode_page(tree, anime, series, season):
    episodes = _merged_series_seasons(tree, anime, series).get(season, {})
    lines = [
        f"🎬 <b>{anime}</b>",
        f"📺 <b>{series}</b>",
        f"📁 <b>{_content_label(season)}</b>",
        "",
    ]
    episode_lines = []
    preferred = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "auto"]

    for episode in sorted(
        episodes,
        key=lambda x: int(x) if str(x).isdigit() else str(x),
    ):
        sources = episodes[episode]
        links = []
        for quality in preferred:
            url = sources.get(quality)
            if url:
                label = "Source" if quality == "auto" else quality
                links.append(f'<a href="{url}">{label}</a>')
        if links:
            episode_lines.append(
                f"🎞️ <b>Episode {episode}</b> — " + " / ".join(links)
            )

    if episode_lines:
        # Episodes are deliberately plain text links, not inline buttons.
        # Telegram HTML blockquote renders the requested quoted episode list.
        lines.append("<blockquote>" + "<br>".join(episode_lines) + "</blockquote>")
    else:
        lines.append("No episodes found.")

    lines.append("")
    lines.append("Tap 480p / 720p / 1080p etc. to open the exact Telegram video.")
    markup = _keyboard([
        [InlineKeyboardButton(
            "⬅️ Back",
            callback_data=f"ls:{_callback_key(anime + '|' + series)}",
        )]
    ])
    return "\n".join(lines), markup

def _resolve_series(tree, key):
    for anime, series_map in tree.items():
        for series in series_map:
            if _callback_key(anime + "|" + series) == key:
                return anime, series
    return None

def _resolve_season(tree, key):
    for anime, series_map in tree.items():
        for series, seasons in series_map.items():
            for season in seasons:
                if _callback_key(anime + "|" + series + "|" + season) == key:
                    return anime, series, season
    return None

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
            key = data[3:]
            anime = next((a for a in tree if _callback_key(a) == key), None)
            if not anime:
                await query.answer("Anime library entry nahi mila.", show_alert=True)
                return
            text, markup = _series_page(tree, anime)
        elif data.startswith("ls:"):
            resolved = _resolve_series(tree, data[3:])
            if not resolved:
                await query.answer("Series library entry nahi mila.", show_alert=True)
                return
            anime, series = resolved
            text, markup = _season_page(tree, anime, series)
        elif data.startswith("lt:"):
            resolved = _resolve_season(tree, data[3:])
            if not resolved:
                await query.answer("Season library entry nahi mila.", show_alert=True)
                return
            anime, series, season = resolved
            text, markup = _episode_page(tree, anime, series, season)
        else:
            return

        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception as edit_exc:
            logger.warning("Library edit failed; sending fresh page: %s", edit_exc)
            await query.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
    except Exception:
        logger.exception("Library navigation failed")
        try:
            await query.answer("Library load failed.", show_alert=True)
        except Exception:
            pass
