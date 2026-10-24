"""The Mirror keeps SQLite and Notion in step: pulls rows in, pushes gallery edits out."""

import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx

from app import db
from app.config import NotionProps
from app.notion import values
from app.notion.client import NotionClient, NotionError
from app.notion.pull import PullResult, parse_page, pull, upsert_row
from app.notion.schema import ensure_schema

# A pull requested within this many seconds of the last one reuses it
# (Notion allows about 3 requests a second).
REUSE_SECONDS = 60

logger = logging.getLogger(__name__)


class PushRejected(Exception):
    """Notion refused an edit outright (a bad value); retrying would never succeed."""


class Mirror:
    """One Notion conversation at a time; the schema is checked before the first."""

    def __init__(self, client: NotionClient, props: NotionProps, db_path: Path):
        self.client = client
        self.props = props
        self.db_path = db_path
        self.error: str | None = None  # why the last pull failed, for /api/health
        self._lock = asyncio.Lock()
        self._caption_kind: str | None = None  # "title" or "rich_text", once the schema is known
        self._last: PullResult | None = None
        self._last_at = 0.0

    async def pull(self, force: bool = False) -> PullResult:
        """Sends edits still waiting for Notion, then reads every row. `force` skips reusing
        a recent pull, as Sync must see Notion as it is now."""
        async with self._lock:
            recent = self._last and time.monotonic() - self._last_at < REUSE_SECONDS
            if recent and not force:
                return self._last
            try:
                await self._ensure_schema()
                with closing(db.connect(self.db_path)) as conn:
                    await self._push_waiting(conn)
                    self._last = await pull(self.client, self.props, conn)
            except Exception as error:
                self.error = str(error)
                raise
            self.error = None
            self._last_at = time.monotonic()
            return self._last

    async def push(self, post_id: int) -> bool:
        """Sends a post's waiting edits now. False when Notion cannot take them yet; they stay
        waiting for the next pull. Raises PushRejected when Notion refuses them for good."""
        async with self._lock:
            await self._ensure_schema()
            with closing(db.connect(self.db_path)) as conn:
                return await self._push_post(conn, post_id)

    async def create_row(self, url: str) -> int:
        """Adds a row for `url` to the Notion database; returns the new post's id."""
        async with self._lock:
            await self._ensure_schema()
            page = await self.client.create_page(
                {self.props.url: values.url(url), self.props.status: values.status("Not started")}
            )
            with closing(db.connect(self.db_path)) as conn, conn:
                return upsert_row(conn, parse_page(page, self.props))

    async def trash(self, page_id: str) -> None:
        """Moves a row to Notion's trash. A row that is already gone counts as trashed."""
        async with self._lock:
            try:
                await self.client.trash_page(page_id)
            except NotionError as error:
                if error.status != 404:
                    raise

    async def _ensure_schema(self) -> None:
        if self._caption_kind is None:
            self._caption_kind = await ensure_schema(self.client, self.props)

    async def _push_waiting(self, conn: sqlite3.Connection) -> None:
        waiting = conn.execute("SELECT DISTINCT post_id FROM pending_edits").fetchall()
        for (post_id,) in waiting:
            try:
                await self._push_post(conn, post_id)
            except PushRejected as error:
                logger.warning("Notion refused the edits to post %s: %s", post_id, error)

    async def _push_post(self, conn: sqlite3.Connection, post_id: int) -> bool:
        pending = conn.execute(
            "SELECT field, edited_at FROM pending_edits WHERE post_id = ?", (post_id,)
        ).fetchall()
        post = conn.execute(
            "SELECT notion_page_id, user_caption, summary FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not pending:
            return True
        if post is None or post["notion_page_id"] is None:
            _forget(conn, post_id, pending)  # local-only: nothing in Notion to update
            return True

        properties = {self._property(field): self._value(conn, post_id, post, field)
                      for field, _ in pending}  # fmt: skip
        try:
            await self.client.update_page(post["notion_page_id"], properties)
        except NotionError as error:
            if error.status in {400, 404}:
                _forget(conn, post_id, pending)
                raise PushRejected(str(error)) from error
            return False
        except httpx.HTTPError:
            return False
        _forget(conn, post_id, pending)
        return True

    def _property(self, field: str) -> str:
        return {
            "caption": self.props.caption,
            "summary": self.props.summary,
            "tags": self.props.tags,
            "ai_tags": self.props.ai_tags,
        }[field]

    def _value(self, conn: sqlite3.Connection, post_id: int, post: sqlite3.Row, field: str):
        if field == "caption":
            text = post["user_caption"]
            return values.title(text) if self._caption_kind == "title" else values.rich_text(text)
        if field == "summary":
            return values.rich_text(post["summary"])
        return values.multi_select(
            db.tag_names(conn, post_id, "notion" if field == "tags" else "ai")
        )


def _forget(conn: sqlite3.Connection, post_id: int, pending: list[sqlite3.Row]) -> None:
    """Drops the edits just sent, unless the field was edited again meanwhile."""
    with conn:
        conn.executemany(
            "DELETE FROM pending_edits WHERE post_id = ? AND field = ? AND edited_at = ?",
            [(post_id, field, edited_at) for field, edited_at in pending],
        )
