"""Pull: copy every Notion row into SQLite and flag rows that left Notion."""

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app import db
from app.config import NotionProps
from app.notion.client import NotionClient
from app.notion.schema import ensure_schema

# A pull requested within this many seconds of the last one reuses it
# (Notion allows about 3 requests a second).
REUSE_SECONDS = 60


@dataclass(frozen=True)
class PullResult:
    pulled: int
    new: int  # rows waiting for Sync: Status is Not started
    deleted_in_notion: int
    finished_at: str


@dataclass(frozen=True)
class NotionRow:
    page_id: str
    url: str
    user_caption: str
    summary: str
    status: str
    error: str
    saved_at: str
    edited_at: str
    tags: list[str]
    ai_tags: list[str]


class Puller:
    """Runs one pull at a time, checking the schema before the first."""

    def __init__(self, client: NotionClient, props: NotionProps, db_path: Path):
        self.client = client
        self.props = props
        self.db_path = db_path
        self.error: str | None = None  # why the last pull failed, for /api/health
        self._lock = asyncio.Lock()
        self._schema_ready = False
        self._last: PullResult | None = None
        self._last_at = 0.0

    async def pull(self) -> PullResult:
        async with self._lock:
            if self._last and time.monotonic() - self._last_at < REUSE_SECONDS:
                return self._last
            try:
                if not self._schema_ready:
                    await ensure_schema(self.client, self.props)
                    self._schema_ready = True
                with closing(db.connect(self.db_path)) as conn:
                    self._last = await pull(self.client, self.props, conn)
            except Exception as error:
                self.error = str(error)
                raise
            self.error = None
            self._last_at = time.monotonic()
            return self._last


async def pull(client: NotionClient, props: NotionProps, conn: sqlite3.Connection) -> PullResult:
    pages, complete = await client.query_all_pages()
    rows = [parse_page(page, props) for page in pages if not page.get("in_trash")]
    with conn:
        for row in rows:
            _upsert(conn, row)
        # A capped result set says nothing about the rows it left out.
        if complete:
            _flag_deleted(conn, {row.page_id for row in rows})
    return PullResult(
        pulled=len(rows),
        new=_count(conn, "notion_state = 'synced' AND status = 'Not started'"),
        deleted_in_notion=_count(conn, "notion_state = 'deleted_in_notion'"),
        finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def parse_page(page: dict, props: NotionProps) -> NotionRow:
    values = page["properties"]
    status = (values.get(props.status) or {}).get("status") or {}
    return NotionRow(
        page_id=page["id"],
        url=(values.get(props.url) or {}).get("url") or "",
        user_caption=_plain_text(values.get(props.caption)),
        summary=_plain_text(values.get(props.summary)),
        # Rows from before the Status property existed have none yet.
        status=status.get("name", "Not started"),
        error=_plain_text(values.get(props.error)),
        saved_at=page["created_time"],
        edited_at=page["last_edited_time"],
        tags=_option_names(values.get(props.tags)),
        ai_tags=_option_names(values.get(props.ai_tags)),
    )


def _upsert(conn: sqlite3.Connection, row: NotionRow) -> None:
    # Notion's values win outright until gallery edits (push) exist; then newest-wins per field.
    post_id = conn.execute(
        """
        INSERT INTO posts (notion_page_id, url, user_caption, summary, status, error,
                           saved_at, notion_edited_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (notion_page_id) DO UPDATE SET
            url = excluded.url,
            user_caption = excluded.user_caption,
            summary = excluded.summary,
            status = excluded.status,
            error = excluded.error,
            saved_at = excluded.saved_at,
            notion_edited_at = excluded.notion_edited_at,
            notion_state = 'synced'
        RETURNING id
        """,
        (
            row.page_id,
            row.url,
            row.user_caption,
            row.summary,
            row.status,
            row.error,
            row.saved_at,
            row.edited_at,
        ),
    ).fetchone()[0]
    _set_tags(conn, post_id, "notion", row.tags)
    _set_tags(conn, post_id, "ai", row.ai_tags)


def _set_tags(conn: sqlite3.Connection, post_id: int, kind: str, names: list[str]) -> None:
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


def _flag_deleted(conn: sqlite3.Connection, seen: set[str]) -> None:
    synced = conn.execute(
        "SELECT id, notion_page_id FROM posts"
        " WHERE notion_state = 'synced' AND notion_page_id IS NOT NULL"
    ).fetchall()
    gone = [(post_id,) for post_id, page_id in synced if page_id not in seen]
    conn.executemany("UPDATE posts SET notion_state = 'deleted_in_notion' WHERE id = ?", gone)


def _count(conn: sqlite3.Connection, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}").fetchone()[0]


def _plain_text(value: dict | None) -> str:
    """Text of a rich_text or title property value."""
    if not value:
        return ""
    return "".join(part["plain_text"] for part in value[value["type"]])


def _option_names(value: dict | None) -> list[str]:
    return [option["name"] for option in (value or {}).get("multi_select", [])]
