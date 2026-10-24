"""SQLite access: one database file in DATA_DIR, schema built up by MIGRATIONS."""

import sqlite3
from pathlib import Path

# Append-only. Entry N upgrades the schema to version N+1, tracked in PRAGMA user_version.
MIGRATIONS: list[str] = [
    # Runtime toggles and sync bookkeeping (auto-pull, last pull time).
    """
    CREATE TABLE app_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # One post per Notion row, mirroring its properties. The pipeline adds its own columns.
    """
    CREATE TABLE posts (
        id               INTEGER PRIMARY KEY,
        notion_page_id   TEXT UNIQUE,  -- NULL once local-only
        url              TEXT NOT NULL DEFAULT '',
        user_caption     TEXT NOT NULL DEFAULT '',
        summary          TEXT NOT NULL DEFAULT '',
        status           TEXT NOT NULL DEFAULT 'Not started' COLLATE NOCASE,
        error            TEXT NOT NULL DEFAULT '',
        saved_at         TEXT NOT NULL,  -- Notion created_time, UTC ISO 8601
        notion_edited_at TEXT,           -- Notion last_edited_time at the last pull
        notion_state     TEXT NOT NULL DEFAULT 'synced'
                         CHECK (notion_state IN ('synced', 'deleted_in_notion', 'local_only'))
    );

    -- Notion tags and AI tags never mix: same name, different kind, different tag.
    CREATE TABLE tags (
        id   INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('notion', 'ai')),
        name TEXT NOT NULL COLLATE NOCASE,
        UNIQUE (kind, name)
    );

    CREATE TABLE post_tags (
        post_id INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
        tag_id  INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
        PRIMARY KEY (post_id, tag_id)
    );
    """,
    # What the Sync pipeline learns and produces. Pulls never touch these columns.
    """
    ALTER TABLE posts ADD COLUMN notion_url TEXT NOT NULL DEFAULT '';
    ALTER TABLE posts ADD COLUMN normalized_url TEXT;  -- the duplicate key; NULL if unusable
    ALTER TABLE posts ADD COLUMN platform TEXT;        -- instagram | youtube | other
    ALTER TABLE posts ADD COLUMN title TEXT NOT NULL DEFAULT '';
    ALTER TABLE posts ADD COLUMN uploader TEXT NOT NULL DEFAULT '';
    ALTER TABLE posts ADD COLUMN platform_caption TEXT NOT NULL DEFAULT '';
    ALTER TABLE posts ADD COLUMN source_transcript TEXT;  -- the platform's subtitles, if any
    ALTER TABLE posts ADD COLUMN transcript TEXT;         -- NULL = not yet; '' = no speech
    ALTER TABLE posts ADD COLUMN thumb_file TEXT;         -- relative to DATA_DIR/media
    ALTER TABLE posts ADD COLUMN media_ready INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE posts ADD COLUMN generated TEXT;  -- JSON summary + AI tags not yet in Notion
    ALTER TABLE posts ADD COLUMN processed_at TEXT;

    CREATE TABLE items (
        id       INTEGER PRIMARY KEY,
        post_id  INTEGER NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        kind     TEXT NOT NULL CHECK (kind IN ('video', 'image')),
        file     TEXT NOT NULL,  -- relative to DATA_DIR/media
        width    INTEGER,
        height   INTEGER,
        duration REAL,
        bytes    INTEGER NOT NULL,
        UNIQUE (post_id, position)
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise


def set_tags(conn: sqlite3.Connection, post_id: int, kind: str, names: list[str]) -> None:
    """Replace a post's tags of one kind ("notion" or "ai")."""
    conn.execute(
        "DELETE FROM post_tags WHERE post_id = ?"
        " AND tag_id IN (SELECT id FROM tags WHERE kind = ?)",
        (post_id, kind),
    )
    for name in names:
        # Tag names match case-insensitively, as in Notion; the latest spelling wins.
        conn.execute(
            "INSERT INTO tags (kind, name) VALUES (?, ?)"
            " ON CONFLICT (kind, name) DO UPDATE SET name = excluded.name",
            (kind, name),
        )
        conn.execute(
            "INSERT OR IGNORE INTO post_tags (post_id, tag_id)"
            " SELECT ?, id FROM tags WHERE kind = ? AND name = ?",
            (post_id, kind, name),
        )
