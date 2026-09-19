import base64
import logging
import re
import unicodedata
import hashlib
from difflib import get_close_matches
from html import escape

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
    # NFKC converts mathematical/bold/italic Unicode alphabets back to normal
    # characters, so users can type anime names in almost any decorative font.
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

    # Prefer the longest known title so "Naruto Shippuden" does not become
    # the shorter "Naruto" title when both words are present.
    for key in sorted(_CANONICAL, key=len, reverse=True):
        if key in norm:
            return _CANONICAL[key]

    if norm.replace(" ", "") == "attackontitan":
        return "Attack on Titan"
    if norm.startswith("ndiaattack on titan") or norm.endswith("attack on titan"):
        return "Attack on Titan"

    # Unknown anime titles are valid. The source topic/name is already the
    # catalog; hard-coding every future anime would make /library stale.
    return re.sub(r"\s+", " ", value or "").strip() or None


def _token(value: str) -> str:
    data = value.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _callback_key(value: str) -> str:
    # Telegram callback_data is limited to 64 bytes. Long anime/series names
    # must still be clickable, so use a deterministic short key.
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _resolve_key(tree, key: str):
    for anime in tree:
        if _callback_key(anime) == key:
            return anime
        for series in tree.get(anime, {}):
            if _callback_key(anime + "|" + series) == key:
                return anime, series
            for season in tree.get(anime, {}).get(series, {}):
                if _callback_key(anime + "|" + series + "|" + season) == key:
                    return anime, series, season
    return None


def _untoken(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def _load_tree():
    tree = {}
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT anime, series, season, episode, quality, source_url
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
        tree.setdefault(anime, {}).setdefault(str(row['series'] or row['anime']), {}).setdefault(season, {}).setdefault(episode, {})[
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
        rows.append([InlineKeyboardButton(f"🎬 {anime}", callback_data=f"la:{_callback_key(anime)}")])
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


def _series_page(tree, anime):
    rows = []
    for series in sorted(tree.get(anime, {}), key=str.casefold):
        rows.append([InlineKeyboardButton(f"📚 {series}", callback_data=f"lr:{_callback_key(anime + "|" + series)}")])
    rows.append([InlineKeyboardButton("⬅️ Anime", callback_data="lb")])
    return f"🎬 <b>{escape(anime)}</b>\n\nChoose series:", _keyboard(rows)


def _season_page(tree, anime, series):
    rows = []
    for season in sorted(tree.get(anime, {}).get(series, {}), key=_season_sort_key):
        rows.append([InlineKeyboardButton(f"📺 {_content_label(season)}", callback_data=f"ls:{_callback_key(anime + "|" + series + "|" + season)}")])
    rows.append([InlineKeyboardButton("⬅️ Series", callback_data=f"lr:{_token(anime + "|" + series)}")])
    title = series if len(tree.get(anime, {})) > 1 else anime
    return f"🎬 <b>{escape(title)}</b>\n\nChoose Season / OVA / Movie:", _keyboard(rows)


def _episode_page(tree, anime, series, season):
    episodes = tree.get(anime, {}).get(series, {}).get(season, {})
    title = series if len(tree.get(anime, {})) > 1 else anime
    lines = [f"🎬 <b>{escape(title)}</b>", f"📺 <b>{escape(_content_label(season))}</b>", ""]
    rows = []

    for episode in sorted(episodes, key=lambda x: int(x) if str(x).isdigit() else str(x)):
        sources = episodes[episode]
        links = _quality_links(sources)
        lines.append(f"🎞️ <b>Episode {escape(episode)}</b> — {links}")

    if len(tree.get(anime, {})) > 1:
        back_data = f"lr:{_callback_key(anime + '|' + series)}"
        back_label = "⬅️ Series"
    else:
        back_data = "lb"
        back_label = "⬅️ Anime"
    rows.append([InlineKeyboardButton(back_label, callback_data=back_data)])
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
            anime = _resolve_key(tree, data[3:])
            if not isinstance(anime, str) or anime not in tree:
                await query.answer("Anime library entry nahi mila.", show_alert=True)
                return
            if len(tree[anime]) > 1:
                text, markup = _series_page(tree, anime)
            else:
                series = next(iter(tree[anime]))
                text, markup = _season_page(tree, anime, series)
        elif data.startswith("lr:"):
            resolved = _resolve_key(tree, data[3:])
            if not isinstance(resolved, tuple) or len(resolved) != 2:
                await query.answer("Series library entry nahi mila.", show_alert=True)
                return
            anime, series = resolved
            if anime not in tree or series not in tree.get(anime, {}):
                await query.answer("Series library entry nahi mila.", show_alert=True)
                return
            text, markup = _season_page(tree, anime, series)
        elif data.startswith("ls:"):
            resolved = _resolve_key(tree, data[3:])
            if not isinstance(resolved, tuple) or len(resolved) != 3:
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
