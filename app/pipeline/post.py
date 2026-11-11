"""One post through the pipeline. Each step checks for its own output first, so a rerun resumes."""

import dataclasses
import json
import re
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
from app.pipeline.content import Download
from app.pipeline.download import Downloader
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

    def cancel(self) -> None:
        """Nothing to pass on: the run's on_step raises Cancelled at the next step."""

    async def close(self) -> None:
        """Nothing to end: the services live as long as the process."""

    async def process(
        self,
        conn: sqlite3.Connection,
        post_id: int,
        on_step: Callable[[str], None],
        skip_sites: Mapping[str, LoginNeeded] = MappingProxyType({}),
    ) -> None:
        """Raises PostFailed once the row is marked Failed, or TryLater to leave it for later.
        `skip_sites` maps each site the run stopped asking to the wall it met there. A preview
        whose video was asked for gets its video instead (see _fetch_video)."""
        if _post(conn, post_id)["want_video"]:
            await self._fetch_video(conn, post_id, on_step, skip_sites)
            return
        try:
            await self._steps(conn, post_id, on_step, skip_sites)
        except PostFailed as failure:
            await self._mark_failed(conn, post_id, str(failure))
            raise

    async def _steps(self, conn, post_id, on_step, skip_sites) -> None:
        post = _post(conn, post_id)
        link = normalise(post["url"])
        if link.site in skip_sites:
            raise skip_sites[link.site].again()
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

    async def _fetch_video(self, conn, post_id: int, on_step, skip_sites) -> None:
        """The video of a preview, asked for in the gallery. The post stays Done and keeps its
        summary and tags, and Notion is not asked; whisper makes the transcript the captions
        did not give. A failure leaves the preview as it was: PostFailed for good, a passing
        one (TryLater) until the next Sync, which tries again."""
        post = _post(conn, post_id)
        link = normalise(post["url"])
        if link.site in skip_sites:
            raise skip_sites[link.site].again()
        if post["kind"] == "preview":
            try:
                await self._fetch_media(conn, post_id, link, on_step, skip_sites, video=True)
            except PostFailed:
                shutil.rmtree(self.work_root / str(post_id), ignore_errors=True)
                with conn:
                    conn.execute("UPDATE posts SET want_video = 0 WHERE id = ?", (post_id,))
                raise

        post = _post(conn, post_id)
        if not post["transcript"]:
            on_step("transcribing")
            transcript = post["source_transcript"] or await self._whisper(conn, post_id)
            with conn:
                conn.execute("UPDATE posts SET transcript = ? WHERE id = ?", (transcript, post_id))
                db.reindex_post(conn, post_id)
        with conn:
            conn.execute("UPDATE posts SET want_video = 0 WHERE id = ?", (post_id,))

    async def _fetch_media(
        self, conn, post_id: int, link: PostUrl, on_step, skip_sites, video: bool = False
    ) -> None:
        work = self.work_root / str(post_id)
        manifest = work / "download.json"
        if not manifest.exists():
            on_step("downloading")
            shutil.rmtree(work, ignore_errors=True)  # leftovers of an interrupted download
            download = await self.services.downloader.download(
                link, work / "raw", skip_sites, video=video
            )
            manifest.write_text(download.to_json())
        download = Download.from_json(manifest.read_text())
        if download.url and download.url != link.url:
            # A share link, now resolved: its post may be saved already under its own link.
            with conn:
                conn.execute(
                    "UPDATE posts SET normalized_url = ? WHERE id = ?", (download.url, post_id)
                )
            _reject_duplicate(
                conn, _post(conn, post_id), dataclasses.replace(link, url=download.url)
            )

        if download.items:
            on_step("compressing")
        else:
            on_step("saving the preview" if download.preview else "saving the page")
        await self._store_media(conn, post_id, work / "raw", download)
        shutil.rmtree(work, ignore_errors=True)

    async def _store_media(self, conn, post_id: int, raw: Path, download: Download) -> None:
        # Made beside the post's files, which stay on show (a preview's thumbnail while its
        # video compresses) until these replace them.
        out = self.media_root / f"{post_id}.new"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        try:
            rows, thumb_file, page_file = await self._convert(post_id, raw, out, download)
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)
            raise
        final = self.media_root / str(post_id)
        shutil.rmtree(final, ignore_errors=True)
        out.rename(final)

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
                " source_transcript = ?, thumb_file = ?, kind = ?, page_file = ?, page_text = ?,"
                " duration = ?, media_ready = 1 WHERE id = ?",
                (
                    download.title,
                    download.uploader,
                    download.caption,
                    download.subtitles,
                    thumb_file,
                    "media" if rows else "preview" if download.preview else "page",
                    page_file,
                    download.page.text if download.page else "",
                    download.duration if download.preview else None,
                    post_id,
                ),
            )
            db.reindex_post(conn, post_id)

    async def _convert(
        self, post_id: int, raw: Path, out: Path, download: Download
    ) -> tuple[list[tuple], str | None, str | None]:
        """Compresses the download into `out`: its items, thumbnail and readable copy."""
        rows = []
        for position, item in enumerate(download.items, start=1):
            stem = out / f"{position:02}"
            if item.kind == "video":
                path = stem.with_suffix(".mp4")
                info = await media.compress_video(raw / item.file, path)
            else:
                path, info = await media.convert_image(raw / item.file, stem)
            rows.append((post_id, position, item.kind, path, info))

        thumb_file = None  # a page's tile is its title, drawn by the gallery
        if rows:
            first_path, first = rows[0][3], rows[0][4]
            # The platform's own thumbnail suits landscape video; vertical video gets its own
            # frame, as YouTube's thumbnails for Shorts are often letterboxed.
            landscape = (first.width or 0) > (first.height or 0)
            thumb_source = (
                raw / download.thumbnail if download.thumbnail and landscape else first_path
            )
            await media.make_thumbnail(thumb_source, out / "thumb.webp")
            thumb_file = f"{post_id}/thumb.webp"
        elif download.preview and download.thumbnail:
            await media.make_thumbnail(raw / download.thumbnail, out / "thumb.webp")
            thumb_file = f"{post_id}/thumb.webp"
        return rows, thumb_file, await self._store_page(post_id, raw, out, download)

    async def _store_page(
        self, post_id: int, raw: Path, out: Path, download: Download
    ) -> str | None:
        """Writes the readable copy, its images converted like any other, and returns its
        file; None when there is no copy to read. An image ffmpeg cannot read is left out."""
        if download.page is None or not download.page.html:
            return None
        html = download.page.html
        for number, name in enumerate(download.page.images, start=1):
            try:
                path, _ = await media.convert_image(raw / name, out / f"page-{number:02}")
            except PostFailed:
                html = re.sub(rf'<img[^>]*src="{re.escape(name)}"[^>]*>', "", html)
                continue
            html = html.replace(f'src="{name}"', f'src="/media/{post_id}/{path.name}"')
        (out / "page.html").write_text(html, encoding="utf-8")
        return f"{post_id}/page.html"

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
            if post["kind"] == "preview" and post["thumb_file"]:  # no frames to take
                src = self.media_root / post["thumb_file"]
                images.append(await media.llm_image(src, scratch / "thumb.jpg"))
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
                    page_text=post["page_text"],
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
                " kind = 'media', page_file = NULL, page_text = '', processed_at = ? WHERE id = ?",
                (message, _now(), post_id),
            )
            db.reindex_post(conn, post_id)

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
