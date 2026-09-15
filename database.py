from pathlib import Path
import sqlite3
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "library.db"

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


def get_connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")

    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime TEXT NOT NULL,
                season TEXT NOT NULL,
                episode TEXT NOT NULL,
                quality TEXT NOT NULL DEFAULT 'auto',
                source_url TEXT NOT NULL,
                language TEXT DEFAULT 'Unknown',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE(
                    anime,
                    season,
                    episode,
                    quality
                )
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_library_anime
            ON library(anime)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_library_episode
            ON library(anime, season, episode)
            """
        )

        conn.commit()


def add_source(
    anime,
    season,
    episode,
    quality,
    source_url,
    language="Unknown",
):
    timestamp = now_iso()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO library (
                anime,
                season,
                episode,
                quality,
                source_url,
                language,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(
                anime,
                season,
                episode,
                quality
            )
            DO UPDATE SET
                source_url = excluded.source_url,
                language = excluded.language,
                updated_at = excluded.updated_at
            """,
            (
                anime,
                str(season),
                str(episode),
                quality,
                source_url,
                language,
                timestamp,
                timestamp,
            ),
        )

        conn.commit()


def anime_exists(anime):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM library
            WHERE LOWER(anime) = LOWER(?)
            LIMIT 1
            """,
            (anime,),
        ).fetchone()

        return row is not None


def get_animes():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT anime
            FROM library
            ORDER BY anime COLLATE NOCASE
            """
        ).fetchall()

        return [r["anime"] for r in rows]


def get_seasons(anime):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT season
            FROM library
            WHERE LOWER(anime) = LOWER(?)
            ORDER BY CAST(season AS INTEGER)
            """,
            (anime,),
        ).fetchall()

        return [r["season"] for r in rows]


def get_episodes(anime, season):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT episode
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
            ORDER BY CAST(episode AS INTEGER)
            """,
            (
                anime,
                str(season),
            ),
        ).fetchall()

        return [r["episode"] for r in rows]


def get_qualities(anime, season, episode):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT quality
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
            ORDER BY
                CASE
                    WHEN quality = '1080p' THEN 1
                    WHEN quality = '720p' THEN 2
                    WHEN quality = '480p' THEN 3
                    ELSE 4
                END
            """,
            (
                anime,
                str(season),
                str(episode),
            ),
        ).fetchall()

        return [r["quality"] for r in rows]


def get_all_sources_for_episode(
    anime,
    season,
    episode,
):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT quality, source_url
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
            """,
            (
                anime,
                str(season),
                str(episode),
            ),
        ).fetchall()

        return {
            r["quality"]: r["source_url"]
            for r in rows
        }


def get_best_source(
    anime,
    season,
    episode,
):
    sources = get_all_sources_for_episode(
        anime,
        season,
        episode,
    )

    if not sources:
        return None

    preferred = [
        "1080p",
        "720p",
        "480p",
        "auto",
    ]

    for quality in preferred:
        if quality in sources:
            return sources[quality]

    return next(iter(sources.values()))


def get_source_url(
    anime,
    season,
    episode,
    quality,
):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT source_url
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
              AND quality = ?
            LIMIT 1
            """,
            (
                anime,
                str(season),
                str(episode),
                quality,
            ),
        ).fetchone()

        return row["source_url"] if row else None


def delete_source(
    anime,
    season,
    episode,
    quality,
):
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
              AND quality = ?
            """,
            (
                anime,
                str(season),
                str(episode),
                quality,
            ),
        )

        conn.commit()


def delete_episode(
    anime,
    season,
    episode,
):
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
            """,
            (
                anime,
                str(season),
                str(episode),
            ),
        )

        conn.commit()


def delete_season(
    anime,
    season,
):
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
            """,
            (
                anime,
                str(season),
            ),
        )

        conn.commit()


init_db()