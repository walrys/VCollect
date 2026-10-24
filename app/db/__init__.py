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
