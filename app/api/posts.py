"""The gallery's data: every post whose media is ready, and keyword search over them."""

from collections import defaultdict
from contextlib import closing

from fastapi import APIRouter, Request

from app import db

router = APIRouter()


@router.get("/posts")
async def list_posts(request: Request) -> dict:
    """All visible posts, newest saved first. The page filters by tag itself, instantly."""
    with closing(db.connect(request.app.state.settings.db_path)) as conn:
        posts = conn.execute(
            "SELECT id, title, user_caption, uploader, platform, url, notion_url, saved_at,"
            " summary, thumb_file, notion_state FROM posts"
            " WHERE media_ready = 1 ORDER BY saved_at DESC, id DESC"
        ).fetchall()
        tags: dict[int, dict[str, list[str]]] = defaultdict(lambda: {"notion": [], "ai": []})
        for post_id, kind, name in conn.execute(
            "SELECT post_tags.post_id, tags.kind, tags.name FROM post_tags"
            " JOIN tags ON tags.id = post_tags.tag_id ORDER BY tags.name"
        ):
            tags[post_id][kind].append(name)
        items: dict[int, list[dict]] = defaultdict(list)
        for item in conn.execute(
            "SELECT post_id, kind, file, width, height, duration FROM items"
            " ORDER BY post_id, position"
        ):
            items[item["post_id"]].append(
                {
                    "kind": item["kind"],
                    "src": f"/media/{item['file']}",
                    "width": item["width"],
                    "height": item["height"],
                    "duration": item["duration"],
                }
            )
    return {
        "posts": [
            {
                "id": post["id"],
                "title": post["title"],
                "caption": post["user_caption"],
                "uploader": post["uploader"],
                "platform": post["platform"],
                "url": post["url"],
                "notion_url": post["notion_url"],
                "saved_at": post["saved_at"],
                "summary": post["summary"],
                "thumb": f"/media/{post['thumb_file']}",
                "state": post["notion_state"],
                "tags": tags[post["id"]]["notion"],
                "ai_tags": tags[post["id"]]["ai"],
                "items": items[post["id"]],
            }
            for post in posts
        ]
    }


@router.get("/search")
async def search(request: Request, q: str = "") -> dict:
    """IDs of the posts matching every word of `q`; the page keeps its own order."""
    with closing(db.connect(request.app.state.settings.db_path)) as conn:
        return {"ids": db.search(conn, q)}
