from pathlib import Path
import sqlite3

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "library.db"


def migrate_library_schema():
    if not DB_PATH.exists():
        return

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='library'").fetchone()
        if not table:
            return

        columns = [row[1] for row in conn.execute("PRAGMA table_info(library)").fetchall()]
        quality_exists = "quality" in columns

        has_quality_unique = False
        for row in conn.execute("PRAGMA index_list(library)").fetchall():
            index_name = row[1]
            is_unique = bool(row[2])
            if not is_unique:
                continue
            index_columns = [r[2] for r in conn.execute(f"PRAGMA index_info({index_name!r})").fetchall()]
            if index_columns == ["anime", "season", "episode", "quality"]:
                has_quality_unique = True
                break

        if quality_exists and has_quality_unique:
            return

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS library_schema_new")
        conn.execute(
            """
            CREATE TABLE library_schema_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime TEXT NOT NULL,
                season TEXT NOT NULL,
                episode TEXT NOT NULL,
                quality TEXT NOT NULL DEFAULT 'auto',
                source_url TEXT NOT NULL,
                language TEXT DEFAULT 'Unknown',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(anime, season, episode, quality)
            )
            """
        )

        if quality_exists:
            conn.execute(
                """
                INSERT OR IGNORE INTO library_schema_new
                (id, anime, season, episode, quality, source_url, language, created_at, updated_at)
                SELECT id, anime, season, episode, COALESCE(quality, 'auto'), source_url,
                       COALESCE(language, 'Unknown'), created_at, updated_at
                FROM library
                """
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO library_schema_new
                (id, anime, season, episode, quality, source_url, language, created_at, updated_at)
                SELECT id, anime, season, episode, 'auto', source_url,
                       COALESCE(language, 'Unknown'), created_at, updated_at
                FROM library
                """
            )

        conn.execute("DROP TABLE library")
        conn.execute("ALTER TABLE library_schema_new RENAME TO library")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_library_anime ON library(anime)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_library_episode ON library(anime, season, episode)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
