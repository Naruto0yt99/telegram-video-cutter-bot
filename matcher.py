from __future__ import annotations

from typing import Any

from database import find_episode, search_library
from search import (
    find_closest_name,
    normalize_space,
    parse_search_query,
)


def _row_to_dict(row: Any) -> dict:
    """Convert sqlite Row / dict-like row into a normal dictionary."""
    if row is None:
        return {}

    if isinstance(row, dict):
        return dict(row)

    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _clean_anime_name(name: str) -> str:
    return normalize_space(name).strip(" -_:|")


def _episode_number(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _same_episode(item: dict, season: int | None, episode: int | None) -> bool:
    item_season = _episode_number(item.get("season"))
    item_episode = _episode_number(item.get("episode"))

    return (
        item_season == season
        and item_episode == episode
    )


def _score_result(
    item: dict,
    requested_anime: str,
    requested_season: int | None,
    requested_episode: int | None,
) -> int:
    """Give a simple score so the best library match comes first."""
    score = 0

    anime = str(item.get("anime") or "")
    normalized_anime = anime.casefold()
    requested = requested_anime.casefold()

    if normalized_anime == requested:
        score += 100
    elif requested in normalized_anime or normalized_anime in requested:
        score += 60

    if requested_season is not None:
        if _episode_number(item.get("season")) == requested_season:
            score += 20

    if requested_episode is not None:
        if _episode_number(item.get("episode")) == requested_episode:
            score += 20

    if item.get("telegram_message_id"):
        score += 3

    if item.get("drive_file_id"):
        score += 2

    return score


def match_findclip(query: str) -> dict:
    """
    Match a /findclip query against the local library.

    Examples:
        Naruto S1 E5
        Naruto Season 2 Episode 10
        Naruto S2E10
    """
    query = normalize_space(query)

    if not query:
        return {
            "ok": False,
            "reason": "empty_query",
            "message": (
                "❌ Query empty hai.\n\n"
                "Example:\n"
                "/findclip Naruto S1 E5"
            ),
        }

    try:
        parsed = parse_search_query(query)
    except Exception as exc:
        return {
            "ok": False,
            "reason": "parse_error",
            "message": f"❌ Search query samajh nahi aayi: {exc}",
        }

    anime = _clean_anime_name(parsed.get("anime", ""))
    season = parsed.get("season")
    episode = parsed.get("episode")

    if not anime:
        return {
            "ok": False,
            "reason": "anime_missing",
            "message": (
                "❌ Anime name nahi mila.\n\n"
                "Example:\n"
                "/findclip Naruto S1 E5"
            ),
        }

    if season is None or episode is None:
        return {
            "ok": False,
            "reason": "season_episode_missing",
            "anime": anime,
            "message": (
                "❌ Season aur episode dono dena zaroori hai.\n\n"
                "Example:\n"
                "/findclip Naruto S1 E5\n"
                "/findclip Naruto S2E10"
            ),
        }

    # First: exact anime + season + episode.
    exact_rows = find_episode(
        anime,
        season,
        episode,
    )

    exact_results = [_row_to_dict(row) for row in exact_rows or []]

    if exact_results:
        exact_results.sort(
            key=lambda item: _score_result(
                item,
                anime,
                season,
                episode,
            ),
            reverse=True,
        )

        return {
            "ok": True,
            "match_type": "exact",
            "query": query,
            "anime": anime,
            "season": season,
            "episode": episode,
            "results": exact_results,
        }

    # If exact anime spelling doesn't match, search the library.
    candidates = search_library(anime)
    candidates = [_row_to_dict(row) for row in candidates or []]

    # Keep only requested season + episode.
    filtered = [
        item
        for item in candidates
        if _same_episode(item, season, episode)
    ]

    if filtered:
        filtered.sort(
            key=lambda item: _score_result(
                item,
                anime,
                season,
                episode,
            ),
            reverse=True,
        )

        return {
            "ok": True,
            "match_type": "library_fuzzy",
            "query": query,
            "anime": anime,
            "season": season,
            "episode": episode,
            "results": filtered,
        }

    # Try to find the closest anime name from the library.
    all_library = search_library(anime)
    all_library = [_row_to_dict(row) for row in all_library or []]

    names = []
    seen = set()

    for item in all_library:
        name = str(item.get("anime") or "").strip()

        if name and name.casefold() not in seen:
            names.append(name)
            seen.add(name.casefold())

    closest = find_closest_name(anime, names)

    if closest and closest.casefold() != anime.casefold():
        closest_rows = find_episode(
            closest,
            season,
            episode,
        )

        closest_results = [
            _row_to_dict(row)
            for row in closest_rows or []
        ]

        if closest_results:
            closest_results.sort(
                key=lambda item: _score_result(
                    item,
                    closest,
                    season,
                    episode,
                ),
                reverse=True,
            )

            return {
                "ok": True,
                "match_type": "closest_name",
                "query": query,
                "anime": closest,
                "requested_anime": anime,
                "season": season,
                "episode": episode,
                "results": closest_results,
            }

    return {
        "ok": False,
        "reason": "not_found",
        "query": query,
        "anime": anime,
        "season": season,
        "episode": episode,
        "message": (
            f"❌ Library me nahi mila:\n"
            f"Anime: {anime}\n"
            f"Season: {season}\n"
            f"Episode: {episode}\n\n"
            "Pehle is episode ko library me add karna hoga."
        ),
    }


def format_match(result: dict) -> str:
    """Create a Telegram-friendly response for a match."""
    if not result.get("ok"):
        return result.get(
            "message",
            "❌ Match nahi mila.",
        )

    results = result.get("results", [])

    if not results:
        return "❌ Match mila, lekin source available nahi hai."

    item = results[0]

    anime = item.get("anime") or result.get("anime", "Unknown")
    season = item.get("season") or result.get("season", "?")
    episode = item.get("episode") or result.get("episode", "?")
    language = item.get("language") or "Unknown"

    sources = []

    if item.get("telegram_chat_id") and item.get("telegram_message_id"):
        sources.append("Telegram")

    if item.get("drive_file_id"):
        sources.append("Google Drive")

    if not sources:
        sources.append("Library record")

    return (
        "✅ Clip source found!\n\n"
        f"🎬 Anime: {anime}\n"
        f"📺 Season: {season}\n"
        f"🎞 Episode: {episode}\n"
        f"🌐 Language: {language}\n"
        f"📦 Source: {', '.join(sources)}"
    )
