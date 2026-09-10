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


def _add_column_if_missing(conn, table, column, definition):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'episode',
                season TEXT DEFAULT '',
                episode TEXT DEFAULT '',
                title TEXT DEFAULT '',
                language TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                telegram_chat_id INTEGER,
                telegram_message_id INTEGER,
                drive_file_id TEXT,
                index_path TEXT DEFAULT '',
                duration REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ready',
                batch_id TEXT DEFAULT '',
                source_order INTEGER DEFAULT 0,
                error_message TEXT DEFAULT '',
                last_attempt_at TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        _add_column_if_missing(conn, "library", "index_path", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "library", "duration", "REAL DEFAULT 0")
        _add_column_if_missing(conn, "library", "status", "TEXT NOT NULL DEFAULT 'ready'")
        _add_column_if_missing(conn, "library", "batch_id", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "library", "source_order", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "library", "error_message", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "library", "last_attempt_at", "TEXT DEFAULT ''")

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_library_anime
            ON library(anime)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_library_episode
            ON library(anime, season, episode, language)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_library_content_type
            ON library(content_type)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_library_status
            ON library(status)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_library_batch
            ON library(batch_id, source_order)
        """)

        conn.commit()


def add_library_item(
    anime,
    content_type,
    season,
    episode,
    title,
    language,
    source_type,
    source_url,
    telegram_chat_id=None,
    telegram_message_id=None,
    drive_file_id=None,
    index_path="",
    duration=0,
    status="ready",
    batch_id="",
    source_order=0,
    error_message="",
    last_attempt_at="",
):
    timestamp = now_iso()

    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO library (
                anime, content_type, season, episode, title, language,
                source_type, source_url, telegram_chat_id,
                telegram_message_id, drive_file_id, index_path,
                duration, status, batch_id, source_order,
                error_message, last_attempt_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            anime, content_type, season, episode, title, language,
            source_type, source_url, telegram_chat_id,
            telegram_message_id, drive_file_id, index_path,
            float(duration or 0), status, batch_id,
            int(source_order or 0), error_message,
            last_attempt_at, timestamp, timestamp
        ))

        conn.commit()
        return cursor.lastrowid


def find_episode(anime, season, episode, language=None):
    with get_connection() as conn:
        if language:
            return conn.execute("""
                SELECT *
                FROM library
                WHERE LOWER(anime) = LOWER(?)
                  AND season = ?
                  AND episode = ?
                  AND LOWER(language) = LOWER(?)
                  AND content_type = 'episode'
                LIMIT 1
            """, (
                anime, str(season), str(episode), language
            )).fetchone()

        return conn.execute("""
            SELECT *
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND episode = ?
              AND content_type = 'episode'
            ORDER BY language
        """, (
            anime, str(season), str(episode)
        )).fetchall()


def search_library(anime):
    with get_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM library
            WHERE LOWER(anime) LIKE LOWER(?)
            ORDER BY
                CASE
                    WHEN season GLOB '[0-9]*'
                    THEN CAST(season AS INTEGER)
                    ELSE 999999
                END,
                CASE
                    WHEN episode GLOB '[0-9]*'
                    THEN CAST(episode AS INTEGER)
                    ELSE 999999
                END,
                language
        """, (f"%{anime}%",)).fetchall()


def list_seasons(anime):
    with get_connection() as conn:
        return conn.execute("""
            SELECT DISTINCT season
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND content_type = 'episode'
            ORDER BY
                CASE
                    WHEN season GLOB '[0-9]*'
                    THEN CAST(season AS INTEGER)
                    ELSE 999999
                END
        """, (anime,)).fetchall()


def list_episodes(anime, season):
    with get_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM library
            WHERE LOWER(anime) = LOWER(?)
              AND season = ?
              AND content_type = 'episode'
            ORDER BY
                CASE
                    WHEN episode GLOB '[0-9]*'
                    THEN CAST(episode AS INTEGER)
                    ELSE 999999
                END,
                language
        """, (anime, str(season))).fetchall()


def get_batch(batch_id):
    with get_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM library
            WHERE batch_id = ?
            ORDER BY source_order, id
        """, (batch_id,)).fetchall()


def get_pending_batch(batch_id):
    with get_connection() as conn:
        return conn.execute("""
            SELECT *
            FROM library
            WHERE batch_id = ?
              AND status != 'ready'
            ORDER BY source_order, id
        """, (batch_id,)).fetchall()


def update_library_item(item_id, **fields):
    allowed = {
        "source_type", "source_url", "telegram_chat_id",
        "telegram_message_id", "drive_file_id", "index_path",
        "duration", "status", "batch_id", "source_order",
        "error_message", "last_attempt_at", "title",
        "language", "season", "episode"
    }

    updates = []
    values = []

    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = ?")
            values.append(value)

    if not updates:
        return False

    updates.append("updated_at = ?")
    values.append(now_iso())
    values.append(item_id)

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE library SET {', '.join(updates)} WHERE id = ?",
            values
        )
        conn.commit()
        return cursor.rowcount > 0


def get_stats():
    with get_connection() as conn:
        stats = {}

        stats["anime"] = conn.execute(
            "SELECT COUNT(DISTINCT anime) FROM library"
        ).fetchone()[0]

        stats["seasons"] = conn.execute("""
            SELECT COUNT(DISTINCT anime || '|' || season)
            FROM library
            WHERE content_type = 'episode'
        """).fetchone()[0]

        stats["episodes"] = conn.execute("""
            SELECT COUNT(*)
            FROM library
            WHERE content_type = 'episode'
              AND status = 'ready'
        """).fetchone()[0]

        stats["movies"] = conn.execute("""
            SELECT COUNT(*)
            FROM library
            WHERE content_type = 'movie'
              AND status = 'ready'
        """).fetchone()[0]

        stats["pending"] = conn.execute("""
            SELECT COUNT(*)
            FROM library
            WHERE status != 'ready'
        """).fetchone()[0]

        stats["languages"] = conn.execute(
            "SELECT COUNT(DISTINCT language) FROM library"
        ).fetchone()[0]

        return stats


init_db()
