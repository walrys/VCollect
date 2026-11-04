"""One post through the pipeline. Each step checks for its own output first, so a rerun resumes."""

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import httpx

from app import db
from app.config import NotionProps
from app.notion import values
from app.notion.client import NotionClient, NotionError
from app.pipeline import media
from app.pipeline.download import Download, Downloader
from app.pipeline.errors import LoginNeeded, PostFailed, TryLater
from app.pipeline.summarise import PostContext, Summariser
from app.pipeline.transcribe import Transcriber
from app.pipeline.urls import PostUrl, normalise

MAX_MODEL_IMAGES = 8
VOCABULARY_SIZE = 150


@dataclass
class Services:
    """The outside tools a post passes through; tests swap in fakes."""

    downloader: Downloader
    transcriber: Transcriber
    summariser: Summariser


@dataclass
class Pipeline:
    services: Services
    notion: NotionClient
    props: NotionProps
    data_dir: Path
    keyframes: int

    @property
    def media_root(self) -> Path:
        return self.data_dir / "media"

    @property
    def work_root(self) -> Path:
        return self.data_dir / "work"

    def index_links(self, conn: sqlite3.Connection) -> None:
        index_links(conn)

    async def process(
        self,
        conn: sqlite3.Connection,
        post_id: int,
        on_step: Callable[[str], None],
        skip_sites: Mapping[str, str] = MappingProxyType({}),
    ) -> None:
        """Raises PostFailed once the row is marked Failed, or TryLater to leave it for later.
        `skip_sites` maps each site the run stopped asking to the reason it stopped."""
        try:
            await self._steps(conn, post_id, on_step, skip_sites)
        except PostFailed as failure:
            await self._mark_failed(conn, post_id, str(failure))
            raise

    async def _steps(self, conn, post_id, on_step, skip_sites) -> None:
        post = _post(conn, post_id)
        link = normalise(post["url"])
        if link.site in skip_sites:
            raise LoginNeeded(link.site, skip_sites[link.site])
        _reject_duplicate(conn, post, link)

        if not post["media_ready"]:
            await self._fetch_media(conn, post_id, link, on_step, skip_sites)

        post = _post(conn, post_id)
        if post["transcript"] is None:
            on_step("transcribing")
            transcript = post["source_transcript"]
            if transcript is None:
                transcript = await self._whisper(conn, post_id)
            with conn:
                conn.execute("UPDATE posts SET transcript = ? WHERE id = ?", (transcript, post_id))
                db.reindex_post(conn, post_id)

        post = _post(conn, post_id)
        if post["generated"] is None:
            on_step("summarising")
            summary, ai_tags = await self._summarise(conn, post)
            generated = json.dumps({"summary": summary, "ai_tags": ai_tags})
            with conn:
                conn.execute("UPDATE posts SET generated = ? WHERE id = ?", (generated, post_id))

        on_step("saving to Notion")
        await self._finish(conn, post_id)

    async def _fetch_media(self, conn, post_id: int, link: PostUrl, on_step, skip_sites) -> None:
        work = self.work_root / str(post_id)
        manifest = work / "download.json"
        if not manifest.exists():
            on_step("downloading")
            shutil.rmtree(work, ignore_errors=True)  # leftovers of an interrupted download
            download = await self.services.downloader.download(link, work / "raw", skip_sites)
            manifest.write_text(download.to_json())
        download = Download.from_json(manifest.read_text())

        on_step("compressing")
        await self._store_media(conn, post_id, work / "raw", download)
        shutil.rmtree(work, ignore_errors=True)

    async def _store_media(self, conn, post_id: int, raw: Path, download: Download) -> None:
        out = self.media_root / str(post_id)
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        rows = []
        for position, item in enumerate(download.items, start=1):
            stem = out / f"{position:02}"
            if item.kind == "video":
                path = stem.with_suffix(".mp4")
                info = await media.compress_video(raw / item.file, path)
            else:
                path, info = await media.convert_image(raw / item.file, stem)
            rows.append((post_id, position, item.kind, path, info))

        first_path, first = rows[0][3], rows[0][4]
        # The platform's own thumbnail suits landscape video; vertical video gets its own frame,
        # as YouTube's thumbnails for Shorts are often letterboxed.
        landscape = (first.width or 0) > (first.height or 0)
        thumb_source = raw / download.thumbnail if download.thumbnail and landscape else first_path
        await media.make_thumbnail(thumb_source, out / "thumb.webp")

        with conn:
            conn.execute("DELETE FROM items WHERE post_id = ?", (post_id,))
            conn.executemany(
                "INSERT INTO items (post_id, position, kind, file, width, height, duration, bytes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (pid, pos, kind, f"{pid}/{path.name}", i.width, i.height, i.duration, i.bytes)
                    for pid, pos, kind, path, i in rows
                ],
            )
            conn.execute(
                "UPDATE posts SET title = ?, uploader = ?, platform_caption = ?,"
                " source_transcript = ?, thumb_file = ?, media_ready = 1 WHERE id = ?",
                (
                    download.title,
                    download.uploader,
                    download.caption,
                    download.subtitles,
                    f"{post_id}/thumb.webp",
                    post_id,
                ),
            )
            db.reindex_post(conn, post_id)

    async def _whisper(self, conn, post_id: int) -> str:
        parts = []
        for item in _items(conn, post_id, "video"):
            with self._scratch() as scratch:
                audio = scratch / "audio.wav"
                if await media.extract_audio(self.media_root / item["file"], audio):
                    parts.append(await self.services.transcriber.transcribe(audio))
        return "\n".join(part for part in parts if part)

    async def _summarise(self, conn, post: sqlite3.Row) -> tuple[str, list[str]]:
        with self._scratch() as scratch:
            images: list[Path] = []
            videos = _items(conn, post["id"], "video")
            if videos and self.keyframes:
                images += await media.keyframes(
                    self.media_root / videos[0]["file"], self.keyframes, scratch
                )
            for index, item in enumerate(_items(conn, post["id"], "image")):
                if len(images) >= MAX_MODEL_IMAGES:
                    break
                src = self.media_root / item["file"]
                images.append(await media.llm_image(src, scratch / f"image-{index}.jpg"))

            vocabulary = [
                name
                for (name,) in conn.execute(
                    "SELECT tags.name FROM tags JOIN post_tags ON post_tags.tag_id = tags.id"
                    " WHERE tags.kind = 'ai' GROUP BY tags.id ORDER BY COUNT(*) DESC LIMIT ?",
                    (VOCABULARY_SIZE,),
                )
            ]
            return await self.services.summariser.summarise(
                PostContext(
                    title=post["title"],
                    uploader=post["uploader"],
                    platform_caption=post["platform_caption"],
                    user_caption=post["user_caption"],
                    transcript=post["transcript"] or "",
                    images=images,
                    vocabulary=vocabulary,
                )
            )

    async def _finish(self, conn, post_id: int) -> None:
        post = _post(conn, post_id)
        generated = json.loads(post["generated"])
        await self._write_notion(
            post["notion_page_id"],
            {
                self.props.summary: values.rich_text(generated["summary"]),
                self.props.ai_tags: values.multi_select(generated["ai_tags"]),
                self.props.status: values.status("Done"),
                self.props.error: values.rich_text(""),
            },
        )
        # Local copies follow Notion only once Notion has them, so a pull cannot undo them.
        with conn:
            conn.execute(
                "UPDATE posts SET summary = ?, status = 'Done', error = '', generated = NULL,"
                " processed_at = ? WHERE id = ?",
                (generated["summary"], _now(), post_id),
            )
            db.set_tags(conn, post_id, "ai", generated["ai_tags"])
            db.reindex_post(conn, post_id)

    async def _mark_failed(self, conn, post_id: int, message: str) -> None:
        post = _post(conn, post_id)
        await self._write_notion(
            post["notion_page_id"],
            {
                self.props.status: values.status("Failed"),
                self.props.error: values.rich_text(message),
            },
        )
        shutil.rmtree(self.work_root / str(post_id), ignore_errors=True)
        shutil.rmtree(self.media_root / str(post_id), ignore_errors=True)
        with conn:
            conn.execute("DELETE FROM items WHERE post_id = ?", (post_id,))
            conn.execute(
                "UPDATE posts SET status = 'Failed', error = ?, media_ready = 0, thumb_file = NULL,"
                " processed_at = ? WHERE id = ?",
                (message, _now(), post_id),
            )

    async def _write_notion(self, page_id: str, properties: dict) -> None:
        try:
            await self.notion.update_page(page_id, properties)
        except (NotionError, httpx.HTTPError) as error:
            raise TryLater(f"Could not update Notion: {error}") from error

    def _scratch(self) -> "_ScratchDir":
        self.work_root.mkdir(parents=True, exist_ok=True)
        return _ScratchDir(self.work_root)


class _ScratchDir(tempfile.TemporaryDirectory):
    """A temporary folder inside DATA_DIR/work, entered as a Path."""

    def __init__(self, parent: Path):
        super().__init__(dir=parent, prefix="scratch-")

    def __enter__(self) -> Path:
        return Path(self.name)


def index_links(conn: sqlite3.Connection) -> None:
    """Give every post its canonical link before processing, so duplicates resolve to the
    earliest saved row whichever order posts are processed in."""
    rows = conn.execute(
        "SELECT id, url FROM posts WHERE normalized_url IS NULL AND url != ''"
    ).fetchall()
    with conn:
        for post_id, url in rows:
            try:
                link = normalise(url)
            except PostFailed:
                continue
            conn.execute(
                "UPDATE posts SET normalized_url = ?, platform = ? WHERE id = ?",
                (link.url, link.platform, post_id),
            )


def _reject_duplicate(conn: sqlite3.Connection, post: sqlite3.Row, link: PostUrl) -> None:
    original = conn.execute(
        "SELECT notion_url, url FROM posts"
        " WHERE normalized_url = ? AND id != ? AND status != 'Failed'"
        " AND notion_state != 'deleted_in_notion'"
        " AND (saved_at < ? OR (saved_at = ? AND id < ?))"
        " ORDER BY saved_at, id LIMIT 1",
        (link.url, post["id"], post["saved_at"], post["saved_at"], post["id"]),
    ).fetchone()
    if original:
        raise PostFailed(f"Duplicate of {original['notion_url'] or original['url']}")


def _post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def _items(conn: sqlite3.Connection, post_id: int, kind: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM items WHERE post_id = ? AND kind = ? ORDER BY position", (post_id, kind)
    ).fetchall()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
