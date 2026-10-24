"""Pull: copy every Notion row into SQLite and flag rows that left Notion."""

import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app import db
from app.config import NotionProps
from app.notion.client import NotionClient

# pending_edits.field -> the NotionRow attribute it overrides.
EDIT_FIELDS = {
    "caption": "user_caption",
    "summary": "summary",
    "tags": "tags",
    "ai_tags": "ai_tags",
}


@dataclass(frozen=True)
class PullResult:
    pulled: int
    new: int  # rows waiting for Sync: Status is Not started
    deleted_in_notion: int
    finished_at: str


@dataclass(frozen=True)
class NotionRow:
    page_id: str
    notion_url: str
    url: str
    user_caption: str
    summary: str
    status: str
    error: str
    saved_at: str
    edited_at: str
    tags: list[str]
    ai_tags: list[str]


async def pull(client: NotionClient, props: NotionProps, conn: sqlite3.Connection) -> PullResult:
    pages, complete = await client.query_all_pages()
    rows = [parse_page(page, props) for page in pages if not page.get("in_trash")]
    with conn:
        for row in rows:
            upsert_row(conn, _keep_newer_local_edits(conn, row))
        # A capped result set says nothing about the rows it left out.
        if complete:
            _flag_deleted(conn, {row.page_id for row in rows})
    return PullResult(
        pulled=len(rows),
        new=count_new(conn),
        deleted_in_notion=_count(conn, "notion_state = 'deleted_in_notion'"),
        finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def parse_page(page: dict, props: NotionProps) -> NotionRow:
    values = page["properties"]
    status = (values.get(props.status) or {}).get("status") or {}
    return NotionRow(
        page_id=page["id"],
        notion_url=page.get("url", ""),
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


def upsert_row(conn: sqlite3.Connection, row: NotionRow) -> int:
    """Write one Notion row into its post, creating the post if new; returns the post id."""
    post_id = conn.execute(
        """
        INSERT INTO posts (notion_page_id, notion_url, url, user_caption, summary, status, error,
                           saved_at, notion_edited_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (notion_page_id) DO UPDATE SET
            notion_url = excluded.notion_url,
            url = excluded.url,
            user_caption = excluded.user_caption,
            summary = excluded.summary,
            status = excluded.status,
            error = excluded.error,
            -- A row re-added to Notion is newer than its post, which keeps its first saved date.
            saved_at = min(posts.saved_at, excluded.saved_at),
            notion_edited_at = excluded.notion_edited_at,
            notion_state = 'synced'
        RETURNING id
        """,
        (
            row.page_id,
            row.notion_url,
            row.url,
            row.user_caption,
            row.summary,
            row.status,
            row.error,
            row.saved_at,
            row.edited_at,
        ),
    ).fetchone()[0]
    db.set_tags(conn, post_id, "notion", row.tags)
    db.set_tags(conn, post_id, "ai", row.ai_tags)
    db.reindex_post(conn, post_id)
    return post_id


def count_new(conn: sqlite3.Connection) -> int:
    return _count(conn, "notion_state = 'synced' AND status = 'Not started'")


def _keep_newer_local_edits(conn: sqlite3.Connection, row: NotionRow) -> NotionRow:
    """Newest edit wins per field: a gallery edit Notion has not received yet beats an older
    Notion value, and a later Notion edit replaces it (Notion times are to the minute)."""
    post = conn.execute(
        "SELECT id, user_caption, summary FROM posts WHERE notion_page_id = ?", (row.page_id,)
    ).fetchone()
    if post is None:
        return row
    pending = conn.execute(
        "SELECT field, edited_at FROM pending_edits WHERE post_id = ?", (post["id"],)
    ).fetchall()
    notion_edited = _time(row.edited_at)
    local: dict = {}
    for field, edited_at in pending:
        if _time(edited_at) >= notion_edited:
            local[EDIT_FIELDS[field]] = _local_value(conn, post, field)
        else:
            conn.execute(
                "DELETE FROM pending_edits WHERE post_id = ? AND field = ?", (post["id"], field)
            )
    return dataclasses.replace(row, **local)


def _local_value(conn: sqlite3.Connection, post: sqlite3.Row, field: str):
    if field == "caption":
        return post["user_caption"]
    if field == "summary":
        return post["summary"]
    kind = "notion" if field == "tags" else "ai"
    return db.tag_names(conn, post["id"], kind)


def _flag_deleted(conn: sqlite3.Connection, seen: set[str]) -> None:
    synced = conn.execute(
        "SELECT id, notion_page_id FROM posts"
        " WHERE notion_state = 'synced' AND notion_page_id IS NOT NULL"
    ).fetchall()
    gone = [(post_id,) for post_id, page_id in synced if page_id not in seen]
    conn.executemany("UPDATE posts SET notion_state = 'deleted_in_notion' WHERE id = ?", gone)


def _count(conn: sqlite3.Connection, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}").fetchone()[0]


def _time(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _plain_text(value: dict | None) -> str:
    """Text of a rich_text or title property value."""
    if not value:
        return ""
    return "".join(part["plain_text"] for part in value[value["type"]])


def _option_names(value: dict | None) -> list[str]:
    return [option["name"] for option in (value or {}).get("multi_select", [])]
