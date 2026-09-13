from pathlib import Path
import sqlite3
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "library.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    """Fresh database schema - no fingerprints, indexes, or old state."""
    with get_connection() as conn:
        # Simple library table: anime sources only
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime TEXT NOT NULL,
                season TEXT NOT NULL,
                episode TEXT NOT NULL,
                quality TEXT NOT NULL,
                source_url TEXT NOT NULL,
                language TEXT DEFAULT 'Unknown',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Create indexes for efficient navigation
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_anime
            ON library(anime)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_season_episode
            ON library(anime, season, episode)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_quality
            ON library(quality)
        """)

        conn.commit()


def add_source(anime, season, episode, quality, source_url, language="Unknown"):
    """Add or replace a source link."""
    timestamp = now_iso()
    with get_connection() as conn:
        # Check if exists
        existing = conn.execute(
            """
            SELECT id FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
              AND quality = ?
            LIMIT 1
            """,
            (anime, str(season), str(episode), quality),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE library
                SET source_url = ?, language = ?, updated_at = ?
                WHERE id = ?
                """,
                (source_url, language, timestamp, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO library
                (anime, season, episode, quality, source_url, language, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (anime, str(season), str(episode), quality, source_url, language, timestamp, timestamp),
            )
        conn.commit()


def get_animes():
    """Get list of all anime titles."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT anime FROM library ORDER BY anime"
        ).fetchall()
        return [row["anime"] for row in rows]


def get_seasons(anime):
    """Get seasons for an anime."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT season FROM library
            WHERE LOWER(anime) = LOWER(?)
            ORDER BY CAST(season AS INTEGER)
            """,
            (anime,),
        ).fetchall()
        return [row["season"] for row in rows]


def get_episodes(anime, season):
    """Get episodes for a season."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT episode FROM library
            WHERE LOWER(anime) = LOWER(?) AND season = ?
            ORDER BY CAST(episode AS INTEGER)
            """,
            (anime, str(season)),
        ).fetchall()
        return [row["episode"] for row in rows]


def get_qualities(anime, season, episode):
    """Get available qualities for an episode."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT quality FROM library
            WHERE LOWER(anime) = LOWER(?) AND season = ? AND episode = ?
            ORDER BY quality DESC
            """,
            (anime, str(season), str(episode)),
        ).fetchall()
        return [row["quality"] for row in rows]


def get_source_url(anime, season, episode, quality):
    """Get source URL for episode + quality."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT source_url FROM library
            WHERE LOWER(anime) = LOWER(?) AND season = ? AND episode = ? AND quality = ?
            LIMIT 1
            """,
            (anime, str(season), str(episode), quality),
        ).fetchone()
        return row["source_url"] if row else None


def get_all_sources_for_episode(anime, season, episode):
    """Get all source links (all qualities) for an episode."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT quality, source_url FROM library
            WHERE LOWER(anime) = LOWER(?) AND season = ? AND episode = ?
            ORDER BY quality DESC
            """,
            (anime, str(season), str(episode)),
        ).fetchall()
        return {row["quality"]: row["source_url"] for row in rows}


def edit_source(anime, season, episode, quality, source_url):
    """Edit a single source link."""
    add_source(anime, season, episode, quality, source_url)


def delete_source(anime, season, episode, quality):
    """Delete a source link."""
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM library
            WHERE LOWER(anime) = LOWER(?) AND season = ? AND episode = ? AND quality = ?
            """,
            (anime, str(season), str(episode), quality),
        )
        conn.commit()


def seed_naruto():
    """Seed Naruto with 9 seasons, 220 episodes (220 available), placeholder URLs."""
    with get_connection() as conn:
        # Clear existing Naruto data
        conn.execute("DELETE FROM library WHERE LOWER(anime) = 'naruto'")
        conn.commit()

    timestamp = now_iso()

    # 9 seasons with episodes
    seasons_data = [
        (1, 220),  # Season 1: 220 episodes (we'll add some)
        (2, 200),
        (3, 150),
        (4, 150),
        (5, 100),
        (6, 150),
        (7, 120),
        (8, 200),
        (9, 500),  # Shippuden has many episodes
    ]

    with get_connection() as conn:
        for season, ep_count in seasons_data:
            # Add first 10 episodes per season for demonstration
            for ep in range(1, min(11, ep_count + 1)):
                for quality in ["480p", "720p", "1080p"]:
                    conn.execute(
                        """
                        INSERT INTO library
                        (anime, season, episode, quality, source_url, language, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "Naruto",
                            str(season),
                            str(ep),
                            quality,
                            f"https://t.me/placeholder/naruto_s{season}_e{ep}_{quality}",
                            "Multi",
                            timestamp,
                            timestamp,
                        ),
                    )
        conn.commit()


init_db()
