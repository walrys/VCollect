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
from app.notion.pull import EDIT_FIELDS, PullResult, parse_page, pull, upsert_row
from app.notion.schema import ensure_schema

# A pull requested within this many seconds of the last one reuses it
# (Notion allows about 3 requests a second).
REUSE_SECONDS = 60
AUTO_PULL_SECONDS = 300
AUTO_PULL = "auto_pull"  # app_settings key: "1" while the menu's auto-pull toggle is on

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

    async def readd(self, post_id: int) -> None:
        """Gives a local-only post a fresh Notion row holding its link, caption, summary, tags
        and status, and links the post to it."""
        async with self._lock:
            await self._ensure_schema()
            with closing(db.connect(self.db_path)) as conn:
                post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
                properties = {
                    self.props.url: values.url(post["url"]),
                    self.props.status: values.status(post["status"]),
                    **{
                        self._property(field): self._value(conn, post_id, post, field)
                        for field in EDIT_FIELDS
                    },
                }
                page = await self.client.create_page(properties)
                with conn:
                    conn.execute(
                        "UPDATE posts SET notion_page_id = ?, notion_url = ?,"
                        " notion_edited_at = ?, notion_state = 'synced' WHERE id = ?",
                        (page["id"], page.get("url", ""), page["last_edited_time"], post_id),
                    )

    async def retry(self, post_ids: list[int]) -> list[int]:
        """Sets Failed rows back to Not started, in Notion and here, so Sync processes them as
        new. Returns the posts reset; posts that are not Failed rows in Notion are left
        alone. When Notion cannot be reached, the posts reset before then stay reset."""
        if not post_ids:
            return []
        async with self._lock:
            await self._ensure_schema()
            with closing(db.connect(self.db_path)) as conn:
                marks = ",".join("?" * len(post_ids))
                failed = conn.execute(
                    "SELECT id, notion_page_id FROM posts WHERE status = 'Failed'"
                    f" AND notion_state = 'synced' AND id IN ({marks})",
                    post_ids,
                ).fetchall()
                reset = []
                for post_id, page_id in failed:
                    try:
                        await self.client.update_page(
                            page_id,
                            {
                                self.props.status: values.status("Not started"),
                                self.props.error: values.rich_text(""),
                            },
                        )
                    except NotionError as error:
                        if error.status == 404:
                            continue  # gone from Notion; the next pull flags it
                        raise
                    with conn:
                        conn.execute(
                            "UPDATE posts SET status = 'Not started', error = '', retry_error = ''"
                            " WHERE id = ?",
                            (post_id,),
                        )
                    reset.append(post_id)
                return reset

    async def auto_pull(self, every: float = AUTO_PULL_SECONDS) -> None:
        """Runs for the app's lifetime, pulling every `every` seconds while auto-pull is on."""
        while True:
            await asyncio.sleep(every)
            with closing(db.connect(self.db_path)) as conn:
                on = db.setting(conn, AUTO_PULL) == "1"
            if not on:
                continue
            try:
                await self.pull()
            except Exception as error:
                # self.error keeps it for /api/health.
                logger.warning("Auto-pull failed: %s", error)

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
