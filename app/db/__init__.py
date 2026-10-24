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
