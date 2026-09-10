from database import (
    add_library_item,
    find_episode,
    get_connection,
    get_stats,
    list_episodes,
    list_seasons,
    search_library,
)


def normalize(value: str) -> str:
    return " ".join((value or "").strip().split())


def save_episode(
    anime: str,
    season: str,
    episode: str,
    language: str,
    source_url: str,
    source_type: str = "telegram",
    telegram_chat_id=None,
    telegram_message_id=None,
    drive_file_id=None,
):
    anime = normalize(anime)
    season = normalize(season)
    episode = normalize(episode)
    language = normalize(language)
    source_url = source_url.strip()

    if not anime:
        raise ValueError("Anime name is required.")

    if not season:
        raise ValueError("Season is required.")

    if not episode:
        raise ValueError("Episode is required.")

    if not language:
        raise ValueError("Language is required.")

    if not source_url:
        raise ValueError("Source link is required.")

    existing = find_episode(
        anime,
        season,
        episode,
        language,
    )

    return {
        "existing": existing,
        "id": None,
        "saved": False,
        "data": {
            "anime": anime,
            "season": season,
            "episode": episode,
            "language": language,
            "source_url": source_url,
            "source_type": source_type,
            "telegram_chat_id": telegram_chat_id,
            "telegram_message_id": telegram_message_id,
            "drive_file_id": drive_file_id,
        },
    }


def replace_episode(
    anime: str,
    season: str,
    episode: str,
    language: str,
    source_url: str,
    source_type: str = "telegram",
    telegram_chat_id=None,
    telegram_message_id=None,
    drive_file_id=None,
):
    timestamp_data = {
        "anime": anime,
        "season": str(season),
        "episode": str(episode),
        "language": language,
    }

    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
              AND LOWER(language) = LOWER(?)
              AND content_type = 'episode'
            LIMIT 1
            """,
            (
                timestamp_data["anime"],
                timestamp_data["season"],
                timestamp_data["episode"],
                timestamp_data["language"],
            ),
        ).fetchone()

        if not existing:
            return add_library_item(
                anime=anime,
                content_type="episode",
                season=str(season),
                episode=str(episode),
                title="",
                language=language,
                source_type=source_type,
                source_url=source_url,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id,
                drive_file_id=drive_file_id,
            )

        conn.execute(
            """
            UPDATE library
            SET source_type = ?,
                source_url = ?,
                telegram_chat_id = ?,
                telegram_message_id = ?,
                drive_file_id = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                source_type,
                source_url,
                telegram_chat_id,
                telegram_message_id,
                drive_file_id,
                existing["id"],
            ),
        )

        conn.commit()

        return existing["id"]


def create_episode(
    anime: str,
    season: str,
    episode: str,
    language: str,
    source_url: str,
    source_type: str = "telegram",
    telegram_chat_id=None,
    telegram_message_id=None,
    drive_file_id=None,
):
    return add_library_item(
        anime=anime,
        content_type="episode",
        season=str(season),
        episode=str(episode),
        title="",
        language=language,
        source_type=source_type,
        source_url=source_url,
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=telegram_message_id,
        drive_file_id=drive_file_id,
    )


def save_movie(
    anime: str,
    title: str,
    language: str,
    source_url: str,
    source_type: str = "telegram",
    drive_file_id=None,
):
    anime = normalize(anime)
    title = normalize(title)
    language = normalize(language)

    if not anime:
        raise ValueError("Anime name is required.")

    if not title:
        raise ValueError("Movie title is required.")

    if not language:
        raise ValueError("Language is required.")

    if not source_url:
        raise ValueError("Source link is required.")

    return add_library_item(
        anime=anime,
        content_type="movie",
        season="",
        episode="",
        title=title,
        language=language,
        source_type=source_type,
        source_url=source_url,
        drive_file_id=drive_file_id,
    )


def get_episode(anime, season, episode, language=None):
    return find_episode(
        anime,
        str(season),
        str(episode),
        language,
    )


def get_anime_seasons(anime):
    return list_seasons(anime)


def get_season_episodes(anime, season):
    return list_episodes(anime, str(season))


def search_anime(anime):
    return search_library(anime)


def library_stats():
    return get_stats()


def delete_episode(anime, season, episode):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
              AND content_type = 'episode'
            """,
            (
                anime,
                str(season),
                str(episode),
            ),
        )

        conn.commit()
        return cursor.rowcount


def delete_season(anime, season):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND content_type = 'episode'
            """,
            (
                anime,
                str(season),
            ),
        )

        conn.commit()
        return cursor.rowcount


def delete_anime(anime):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
            """,
            (anime,),
        )

        conn.commit()
        return cursor.rowcount


def delete_movie(anime, title):
    with get_connection() as conn:
        cursor = conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND LOWER(title) = LOWER(?)
              AND content_type = 'movie'
            """,
            (anime, title),
        )

        conn.commit()
        return cursor.rowcount
