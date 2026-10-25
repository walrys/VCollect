"""The Processing page: the Notion rows Sync has not finished, why, and retrying failed ones."""

from contextlib import closing

from fastapi import APIRouter, HTTPException, Request
from httpx import HTTPError
from pydantic import BaseModel

from app import db
from app.notion.client import NotionError
from app.notion.pull import count_new
from app.notion.schema import SchemaError

router = APIRouter()


class Retry(BaseModel):
    ids: list[int]


@router.get("/processing")
async def processing(request: Request) -> dict:
    """Every row Sync has not finished, newest saved first, each in one stage: failed (skipped
    until retried; the reason is Notion's Error), later (left Not started by the last Sync, which
    the next one retries) or new (no Sync has tried it yet)."""
    with closing(db.connect(request.app.state.settings.db_path)) as conn:
        rows = conn.execute(
            """
            SELECT id, url, user_caption, title, platform, notion_url, saved_at,
                   coalesce(tried_at, processed_at) AS tried_at,
                   CASE WHEN status = 'Failed' THEN 'failed'
                        WHEN retry_error != '' THEN 'later' ELSE 'new' END AS stage,
                   CASE WHEN status = 'Failed' THEN error ELSE retry_error END AS reason
            FROM posts
            WHERE notion_state = 'synced' AND status IN ('Not started', 'Failed')
            ORDER BY saved_at DESC, id DESC
            """
        ).fetchall()
        done = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE notion_state = 'synced' AND status = 'Done'"
        ).fetchone()[0]
        return {
            "rows": [
                {
                    "id": row["id"],
                    "url": row["url"],
                    "caption": row["user_caption"],
                    "title": row["title"],
                    "platform": row["platform"],
                    "notion_url": row["notion_url"],
                    "saved_at": row["saved_at"],
                    "tried_at": row["tried_at"],
                    "stage": row["stage"],
                    "reason": row["reason"],
                }
                for row in rows
            ],
            "done": done,
            "new": count_new(conn),
        }


@router.post("/processing/retry")
async def retry(choice: Retry, request: Request) -> dict:
    """Sets the chosen Failed rows back to Not started, in Notion and here; the next Sync
    processes them as new. Rows that are not Failed are left alone."""
    mirror = request.app.state.mirror
    if mirror is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    try:
        reset = await mirror.retry(choice.ids)
    except (SchemaError, NotionError, HTTPError) as error:
        raise HTTPException(502, f"Could not reset every row in Notion: {error}") from error
    with closing(db.connect(request.app.state.settings.db_path)) as conn:
        return {"reset": reset, "new": count_new(conn)}
