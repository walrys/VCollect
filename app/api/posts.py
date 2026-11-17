"""The gallery's posts: list and search them, edit them, delete them, add new links.
Every Notion row is a post from its first pull on; until Sync has fetched it, it shows as a
placeholder with its status."""

import hashlib
import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from httpx import HTTPError
from pydantic import BaseModel

from app import db
from app.notion.client import NotionError
from app.notion.mirror import PushRejected
from app.notion.pull import count_new
from app.notion.schema import SchemaError
from app.pipeline.errors import PostFailed
from app.pipeline.urls import PostUrl, canonical, index_links, link_changed, normalise
from app.pipeline.web import cookie_status

MAX_TAG_LENGTH = 100
MAX_TAGS = 100

router = APIRouter()


class PostEdit(BaseModel):
    """Only the fields sent are changed. Tags are the full new list."""

    caption: str | None = None
    summary: str | None = None
    tags: list[str] | None = None
    ai_tags: list[str] | None = None
    url: str | None = None  # the saved link; a post with media then offers a resync
    force: bool = False  # save the link even though another post has it already


class NewLink(BaseModel):
    url: str
    caption: str = ""  # the user's note
    tags: list[str] = []  # Notion tags, brand-new ones included
    force: bool = False  # add it even though the link is already saved


class DeletedChoice(BaseModel):
    """The answer to the dialog before Sync about posts whose Notion rows are gone."""

    delete: list[int] = []
    keep: list[int] = []


@router.get("/posts")
async def list_posts(request: Request) -> dict:
    """Every post, newest saved first. The page filters by tag itself, instantly."""
    with closing(_connect(request)) as conn:
        return {"posts": _load(conn, "1")}


@router.get("/search")
async def search(request: Request, q: str = "") -> dict:
    """IDs of the posts matching every word of `q`; the page keeps its own order."""
    with closing(_connect(request)) as conn:
        return {"ids": db.search(conn, q)}


@router.get("/posts/{post_id}/page")
async def page(post_id: int, request: Request) -> dict:
    """The post's readable copy: sanitised HTML whose images are under /media."""
    with closing(_connect(request)) as conn:
        row = conn.execute("SELECT page_file FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None or not row["page_file"]:
        raise HTTPException(404, "This post has no readable copy")
    path = request.app.state.settings.data_dir / "media" / row["page_file"]
    try:
        return {"html": path.read_text(encoding="utf-8")}
    except FileNotFoundError as error:
        raise HTTPException(404, "This post has no readable copy") from error


@router.get("/posts/{post_id}/cookies")
async def cookies(post_id: int, request: Request) -> dict:
    """Whether cookies.txt holds a session for the post's site (missing, expired or present),
    read from the file alone: the site is not asked."""
    with closing(_connect(request)) as conn:
        row = conn.execute("SELECT url FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such post")
    try:
        url = normalise(row["url"]).url
    except PostFailed as error:
        raise HTTPException(422, str(error)) from error
    return cookie_status(request.app.state.settings.cookies_path, url)


@router.post("/posts/{post_id}/video")
async def download_video(post_id: int, request: Request) -> dict:
    """Downloads the video of a preview (a YouTube video Sync saved only the thumbnail of) at
    once: next in a running Sync, else in a run of its own. The post keeps its summary and
    tags; it becomes a media post when the video lands."""
    syncer = request.app.state.syncer
    if syncer is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    with closing(_connect(request)) as conn:
        post = _post_or_404(conn, post_id)
    if link_changed(post["url"], post["fetched_url"]):
        raise HTTPException(409, "Its link changed since: Resync fetches the post first.")
    if not syncer.download_video(post_id):
        raise HTTPException(409, "Only a video saved as a preview can be downloaded.")
    with closing(_connect(request)) as conn:
        return {"post": _load(conn, "posts.id = ?", (post_id,))[0]}


@router.post("/posts/{post_id}/resync")
async def resync(post_id: int, request: Request) -> dict:
    """Fetches a post whose link changed afresh from the new link, at once: next in a running
    Sync, else in a run of its own. Its media stays on show until the new media replaces it;
    its transcript, summary and AI tags are made again, and its Notion row goes back to Not
    started until then. Should that fail for good, the row is Failed and the old media stays."""
    syncer = request.app.state.syncer
    if syncer is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    with closing(_connect(request)) as conn:
        post = _post_or_404(conn, post_id)
    if post["notion_state"] != "synced":
        raise HTTPException(409, "Only posts in Notion can be resynced.")
    if not link_changed(post["url"], post["fetched_url"]):
        raise HTTPException(409, "Its link has not changed since its media was fetched.")
    _link_or_422(post["url"])
    try:
        started = await syncer.resync(post_id)
    except (SchemaError, NotionError, HTTPError) as error:
        raise HTTPException(502, f"Could not set the row back in Notion: {error}") from error
    if not started:
        raise HTTPException(409, "It is being processed right now, or is gone from Notion.")
    with closing(_connect(request)) as conn:
        return {"post": _load(conn, "posts.id = ?", (post_id,))[0], "new": count_new(conn)}


@router.patch("/posts/{post_id}")
async def edit_post(post_id: int, edit: PostEdit, request: Request) -> dict:
    """Saves locally at once, then sends the change to Notion. `notion` in the answer says how
    that went: saved, waiting (Notion unreachable; the next pull sends it), local (the post is
    not in Notion), rejected, or unchanged. A new link must be a link (422), and one another
    post has already is refused (409, naming it) unless `force`."""
    tags = None if edit.tags is None else _clean_tags(edit.tags, lower=False)
    ai_tags = None if edit.ai_tags is None else _clean_tags(edit.ai_tags, lower=True)
    with closing(_connect(request)) as conn:
        post = _post_or_404(conn, post_id)
        url = None if edit.url is None or edit.url.strip() == post["url"] else edit.url.strip()
        if url is not None:
            key = _link_or_422(url).url
            duplicate = _duplicate(conn, key, post_id)
            if duplicate and not edit.force:
                raise _duplicate_error(duplicate)
        changed = _apply(conn, post, edit, tags, ai_tags, url)

    notion, message = "unchanged", ""
    mirror = request.app.state.mirror
    if changed and (post["notion_state"] != "synced" or mirror is None):
        notion = "local"
    elif changed:
        try:
            notion = "saved" if await mirror.push(post_id) else "waiting"
        except PushRejected as error:
            notion, message = "rejected", str(error)
        except (SchemaError, NotionError, HTTPError) as error:
            notion, message = "waiting", str(error)
    with closing(_connect(request)) as conn:
        return {
            "post": _load(conn, "posts.id = ?", (post_id,))[0],
            "notion": notion,
            "message": message,
        }


@router.delete("/posts/{post_id}")
async def delete_post(post_id: int, request: Request) -> dict:
    """Moves the Notion row to Notion's trash, then deletes the post and its files here.
    Nothing is deleted when Notion cannot be reached. A post that is not in Notion (deleted
    there, or local-only) is only deleted here."""
    with closing(_connect(request)) as conn:
        post = _post_or_404(conn, post_id)
    if post["notion_state"] == "synced":
        mirror = request.app.state.mirror
        if mirror is None:
            raise HTTPException(409, "Notion is not configured, so its row cannot be trashed.")
        try:
            await mirror.trash(post["notion_page_id"])
        except (NotionError, HTTPError) as error:
            raise HTTPException(
                502, f"Could not reach Notion; nothing was deleted: {error}"
            ) from error

    with closing(_connect(request)) as conn:
        _delete_here(conn, request.app.state.settings.data_dir, post_id)
        return {"deleted": post_id, "new": count_new(conn)}


@router.post("/posts/deleted-in-notion")
async def resolve_deleted(choice: DeletedChoice, request: Request) -> dict:
    """Deletes the posts in `delete` with their files; the posts in `keep` become local-only
    and are never asked about again. Only posts flagged deleted-in-Notion are touched (a pull
    since the dialog opened may have seen one restored), and Notion is not asked: their rows
    are already gone from it."""
    with closing(_connect(request)) as conn:
        flagged = {
            post_id
            for (post_id,) in conn.execute(
                "SELECT id FROM posts WHERE notion_state = 'deleted_in_notion'"
            )
        }
        deleted = [post_id for post_id in dict.fromkeys(choice.delete) if post_id in flagged]
        kept = [
            post_id
            for post_id in dict.fromkeys(choice.keep)
            if post_id in flagged and post_id not in deleted
        ]
        for post_id in deleted:
            _delete_here(conn, request.app.state.settings.data_dir, post_id)
        with conn:
            conn.executemany(
                "UPDATE posts SET notion_state = 'local_only', notion_page_id = NULL,"
                " notion_url = '' WHERE id = ?",
                [(post_id,) for post_id in kept],
            )
            conn.executemany(
                "DELETE FROM pending_edits WHERE post_id = ?", [(post_id,) for post_id in kept]
            )
    return {"deleted": deleted, "kept": kept}


@router.post("/posts/{post_id}/readd")
async def readd(post_id: int, request: Request) -> dict:
    """Gives a local-only post a fresh Notion row holding its link, caption, summary and
    tags. The post keeps its saved date, although Notion dates the new row today."""
    mirror = request.app.state.mirror
    if mirror is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    with closing(_connect(request)) as conn:
        post = _post_or_404(conn, post_id)
    if post["notion_state"] != "local_only":
        raise HTTPException(409, "Only posts kept after leaving Notion can be re-added.")
    try:
        await mirror.readd(post_id)
    except (SchemaError, NotionError, HTTPError) as error:
        raise HTTPException(502, f"Could not add the row to Notion: {error}") from error
    with closing(_connect(request)) as conn:
        return {"post": _load(conn, "posts.id = ?", (post_id,))[0], "new": count_new(conn)}


@router.post("/posts", status_code=201)
async def add_link(link: NewLink, request: Request) -> dict:
    """Adds a row for the link to Notion, with its caption and Notion tags; the next Sync
    processes it."""
    mirror = request.app.state.mirror
    if mirror is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    key = _link_or_422(link.url).url
    tags = _clean_tags(link.tags, lower=False)

    with closing(_connect(request)) as conn:
        duplicate = _duplicate(conn, key)
    if duplicate and not link.force:
        raise _duplicate_error(duplicate)

    try:
        post_id = await mirror.create_row(link.url.strip(), link.caption.strip(), tags)
    except (SchemaError, NotionError, HTTPError) as error:
        raise HTTPException(502, f"Could not add the row to Notion: {error}") from error
    with closing(_connect(request)) as conn:
        index_links(conn)
        return {"created": post_id, "new": count_new(conn)}


def _link_or_422(url: str) -> PostUrl:
    try:
        return normalise(url)
    except PostFailed as error:
        raise HTTPException(422, str(error)) from error


def _duplicate(conn: sqlite3.Connection, key: str, other_than: int = 0) -> sqlite3.Row | None:
    """The first saved post with the canonical link `key`, other than post `other_than`."""
    index_links(conn)
    return conn.execute(
        "SELECT id, user_caption, title, url FROM posts WHERE normalized_url = ? AND id != ?"
        " ORDER BY saved_at LIMIT 1",
        (key, other_than),
    ).fetchone()


def _duplicate_error(duplicate: sqlite3.Row) -> HTTPException:
    return HTTPException(
        409,
        {
            "message": "This link is already saved.",
            "duplicate": {
                "id": duplicate["id"],
                "caption": duplicate["user_caption"] or duplicate["title"],
                "url": duplicate["url"],
            },
        },
    )


def _apply(conn, post: sqlite3.Row, edit: PostEdit, tags, ai_tags, url: str | None) -> list[str]:
    """Writes the edit and, for posts in Notion, marks the changed fields as waiting for it.
    `url` is the new link, already checked; None leaves the link as it is."""
    post_id = post["id"]
    changed = []
    with conn:
        if url is not None:
            key, platform = canonical(url)
            conn.execute(
                "UPDATE posts SET url = ?, normalized_url = ?, platform = ? WHERE id = ?",
                (url, key, platform, post_id),
            )
            changed.append("url")
        if edit.caption is not None and edit.caption.strip() != post["user_caption"]:
            conn.execute(
                "UPDATE posts SET user_caption = ? WHERE id = ?", (edit.caption.strip(), post_id)
            )
            changed.append("caption")
        if edit.summary is not None and edit.summary.strip() != post["summary"]:
            conn.execute(
                "UPDATE posts SET summary = ? WHERE id = ?", (edit.summary.strip(), post_id)
            )
            changed.append("summary")
        for field, kind, names in (("tags", "notion", tags), ("ai_tags", "ai", ai_tags)):
            if names is not None and _key(names) != _key(db.tag_names(conn, post_id, kind)):
                db.set_tags(conn, post_id, kind, names)
                changed.append(field)
        if not changed:
            return []
        db.reindex_post(conn, post_id)
        if post["notion_state"] == "synced":
            edited_at = datetime.now(UTC).isoformat(timespec="seconds")
            conn.executemany(
                "INSERT INTO pending_edits (post_id, field, edited_at) VALUES (?, ?, ?)"
                " ON CONFLICT (post_id, field) DO UPDATE SET edited_at = excluded.edited_at",
                [(post_id, field, edited_at) for field in changed],
            )
    return changed


def _delete_here(conn: sqlite3.Connection, data_dir: Path, post_id: int) -> None:
    """Deletes a post and its files from this app only."""
    shutil.rmtree(data_dir / "media" / str(post_id), ignore_errors=True)
    shutil.rmtree(data_dir / "work" / str(post_id), ignore_errors=True)
    with conn:
        conn.execute("DELETE FROM post_search WHERE rowid = ?", (post_id,))
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))  # items and tags cascade


def _clean_tags(names: list[str], lower: bool) -> list[str]:
    clean: list[str] = []
    for raw in names:
        name = " ".join(raw.split())
        name = name.lower() if lower else name
        if not name or name.lower() in {tag.lower() for tag in clean}:
            continue
        if "," in name:
            raise HTTPException(422, f'Notion tags cannot contain commas: "{name}"')
        if len(name) > MAX_TAG_LENGTH:
            raise HTTPException(422, f"Tags are limited to {MAX_TAG_LENGTH} characters")
        clean.append(name)
    if len(clean) > MAX_TAGS:
        raise HTTPException(422, f"A post can have at most {MAX_TAGS} tags of each kind")
    return clean


def _key(names: list[str]) -> list[str]:
    return sorted(name.lower() for name in names)


def _post_or_404(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    post = conn.execute(
        "SELECT id, notion_page_id, notion_state, user_caption, summary, url, fetched_url"
        " FROM posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if post is None:
        raise HTTPException(404, "No such post")
    return post


def _connect(request: Request) -> sqlite3.Connection:
    return db.connect(request.app.state.settings.db_path)


def _load(conn: sqlite3.Connection, where: str, params: tuple = ()) -> list[dict]:
    posts = conn.execute(
        "SELECT id, title, user_caption, uploader, platform_caption, url, notion_url, saved_at,"
        " summary, thumb_file, notion_state, status, error, retry_error, needs_login,"
        " media_ready, kind, page_file, duration, want_video, fetched_url"
        f" FROM posts WHERE {where}"
        " ORDER BY saved_at DESC, id DESC",
        params,
    ).fetchall()
    ids = [post["id"] for post in posts]
    versions = {post["id"]: _version(post["fetched_url"]) for post in posts}
    marks = ",".join("?" * len(ids))
    tags: dict[int, dict[str, list[str]]] = defaultdict(lambda: {"notion": [], "ai": []})
    for post_id, kind, name in conn.execute(
        "SELECT post_tags.post_id, tags.kind, tags.name FROM post_tags"
        f" JOIN tags ON tags.id = post_tags.tag_id WHERE post_tags.post_id IN ({marks})"
        " ORDER BY tags.name",
        ids,
    ):
        tags[post_id][kind].append(name)
    items: dict[int, list[dict]] = defaultdict(list)
    for item in conn.execute(
        "SELECT post_id, kind, file, width, height, duration FROM items"
        f" WHERE post_id IN ({marks}) ORDER BY post_id, position",
        ids,
    ):
        items[item["post_id"]].append(
            {
                "kind": item["kind"],
                "src": f"/media/{item['file']}{versions[item['post_id']]}",
                "width": item["width"],
                "height": item["height"],
                "duration": item["duration"],
            }
        )
    return [
        {
            "id": post["id"],
            "title": post["title"],
            "caption": post["user_caption"],
            "uploader": post["uploader"],
            "platform": _platform(post["url"]),
            "url": post["url"],
            "notion_url": post["notion_url"],
            "saved_at": post["saved_at"],
            "summary": post["summary"],
            # media: a tile from its video or image; page: a title card, with a readable copy
            # (`page`) unless it needs JavaScript; preview: a video left on its site, shown by
            # its thumbnail until downloaded; placeholder: nothing fetched yet.
            "kind": post["kind"] if post["media_ready"] else "placeholder",
            "status": _status(post),
            "reason": post["error"] if post["status"] == "Failed" else post["retry_error"],
            "thumb": (
                f"/media/{post['thumb_file']}{versions[post['id']]}" if post["thumb_file"] else None
            ),
            "page": f"/api/posts/{post['id']}/page" if post["page_file"] else None,
            "description": post["platform_caption"] if post["kind"] == "page" else "",
            "duration": post["duration"],  # a preview's length, in seconds
            # A preview's video was asked for: it downloads now, or with the next Sync.
            "video_wanted": bool(post["want_video"]),
            # The link its media came from (canonical), and whether its link has moved on since:
            # the media stays on show until a resync replaces it.
            "fetched_url": post["fetched_url"],
            "link_changed": link_changed(post["url"], post["fetched_url"]),
            "state": post["notion_state"],
            "tags": tags[post["id"]]["notion"],
            "ai_tags": tags[post["id"]]["ai"],
            "items": items[post["id"]],
        }
        for post in posts
    ]


def _version(fetched_url: str | None) -> str:
    """A query naming the link a post's media came from. A resync's files take the old ones'
    names, and without it a browser could go on showing the old ones from its cache."""
    if not fetched_url:
        return ""
    return "?v=" + hashlib.sha1(fetched_url.encode()).hexdigest()[:8]


def _status(post: sqlite3.Row) -> str:
    """done, failed, or why it is not processed yet: new (no Sync has tried it), login (a
    session in cookies.txt would let it through) or waiting (for a passing problem)."""
    if post["status"] == "Done":
        return "done"
    if post["status"] == "Failed":
        return "failed"
    if not post["retry_error"]:
        return "new"
    return "login" if post["needs_login"] else "waiting"


def _platform(url: str) -> str | None:
    """instagram, youtube, reddit or other, for the site's logo; None for a row with no link."""
    try:
        return normalise(url).platform
    except PostFailed:
        return None
