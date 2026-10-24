"""Downloaders: yt-dlp for videos, gallery-dl for Instagram image posts and carousels."""

import asyncio
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import yt_dlp

from app.pipeline.errors import LoginNeeded, PostFailed, TryLater
from app.pipeline.transcribe import vtt_to_text
from app.pipeline.urls import PostUrl

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# gallery-dl needs a login where yt-dlp does not, so its login wall gets its own site name:
# it must never stop the run from fetching the reels yt-dlp manages without one.
INSTAGRAM_IMAGES = "Instagram image posts"

# Lower-cased fragments of downloader errors.
LOGIN_HINTS = ("login", "log in", "sign in", "authentication is required", "--cookies", "401")
GONE_HINTS = (
    "private",
    "unavailable",
    "has been removed",
    "not available",
    "does not exist",
    "not found",
    "could not be found",
    "notfounderror",
    "404",
    "unsupported url",
    "no video formats found",
    "no video in this post",
    "account has been terminated",
)


@dataclass
class RawItem:
    file: str  # relative to the download folder
    kind: str  # video | image


@dataclass
class Download:
    items: list[RawItem]
    title: str
    uploader: str
    caption: str
    subtitles: str | None  # the platform's own subtitles as plain text
    thumbnail: str | None  # relative to the download folder

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Download":
        data = json.loads(text)
        return cls(**{**data, "items": [RawItem(**item) for item in data["items"]]})


class NotASingleVideo(Exception):
    """yt-dlp found an Instagram image post or carousel, which gallery-dl handles."""


class Downloader:
    def __init__(self, cookies: Path, max_duration_min: int = 0):
        self.cookies = cookies
        self.max_duration_min = max_duration_min

    async def download(self, post: PostUrl, dest: Path) -> Download:
        dest.mkdir(parents=True, exist_ok=True)
        try:
            return await asyncio.to_thread(self._yt_dlp, post, dest)
        except NotASingleVideo:
            shutil.rmtree(dest)
            dest.mkdir(parents=True)
            return await self._gallery_dl(post, dest)

    async def _gallery_dl(self, post: PostUrl, dest: Path) -> Download:
        args = ["gallery-dl", "--directory", str(dest), "--filename", "{num:>02}.{extension}"]
        args += ["--write-metadata", "--no-mtime"]
        if self.cookies.exists():
            args += ["--cookies", str(self.cookies)]
        process = await asyncio.create_subprocess_exec(
            *args, post.url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        output = (await process.communicate())[0].decode(errors="replace")
        items = _media_in(dest)
        if process.returncode != 0 or not items:
            raise classify(output, INSTAGRAM_IMAGES)

        metadata_files = sorted(dest.glob("*.json"))
        metadata = json.loads(metadata_files[0].read_text()) if metadata_files else {}
        return Download(
            items=items,
            title="",
            uploader=metadata.get("fullname") or metadata.get("username") or "",
            caption=metadata.get("description") or "",
            subtitles=None,
            thumbnail=None,
        )

    def _yt_dlp(self, post: PostUrl, dest: Path) -> Download:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "format": "bv*+ba/b",
            # Largest stream whose shorter side is at most 720 px: compression keeps it there.
            "format_sort": ["res:720"],
            "merge_output_format": "mp4",
            "outtmpl": {"default": str(dest / "01.%(ext)s")},
            "writethumbnail": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "vtt",
        }
        if self.cookies.exists():
            options["cookiefile"] = str(self.cookies)
        if post.platform == "instagram":
            # Instagram blocks anonymous clients that ask too fast; a backlog is hundreds of reels.
            options |= {"sleep_interval": 3, "max_sleep_interval": 8}
        try:
            info = self._yt_dlp_fetch(post, options)
        except yt_dlp.utils.DownloadError as error:
            if post.platform == "instagram" and "no video formats found" in str(error).lower():
                raise NotASingleVideo from error
            if "subtitles" not in str(error).lower():
                raise classify(str(error), post.site) from error
            # Subtitles are a bonus (YouTube rate-limits them on their own); whisper covers.
            options = {**options, "writesubtitles": False, "writeautomaticsub": False}
            try:
                info = self._yt_dlp_fetch(post, options)
            except yt_dlp.utils.DownloadError as retry_error:
                raise classify(str(retry_error), post.site) from retry_error

        items = [item for item in _media_in(dest) if item.kind == "video"]
        if not items:
            raise PostFailed("No video in this post")
        thumbnails = [path for path in sorted(dest.iterdir()) if path.suffix in IMAGE_EXTENSIONS]
        subtitle_files = sorted(dest.glob("*.vtt"))
        return Download(
            items=items,
            title=info.get("title") or "",
            uploader=info.get("uploader") or info.get("channel") or "",
            caption=info.get("description") or "",
            subtitles=vtt_to_text(subtitle_files[0].read_text()) if subtitle_files else None,
            thumbnail=thumbnails[0].name if thumbnails else None,
        )

    def _yt_dlp_fetch(self, post: PostUrl, options: dict) -> dict:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(post.url, download=False, process=False)
            if info.get("_type") in {"playlist", "multi_video"}:
                if post.platform == "instagram":
                    raise NotASingleVideo  # a carousel
                raise PostFailed("Not a single post: a playlist or channel")
            minutes = (info.get("duration") or 0) / 60
            if self.max_duration_min and minutes > self.max_duration_min:
                raise PostFailed(
                    f"Too long: {minutes:.0f} min (MAX_DURATION_MIN is {self.max_duration_min})"
                )
            return ydl.process_ie_result(info, download=True)


def classify(output: str, site: str) -> TryLater | PostFailed:
    """Turn a downloader's error output into the matching pipeline error."""
    text = output.lower()
    if any(hint in text for hint in LOGIN_HINTS):
        return LoginNeeded(site)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    last = next(
        (line for line in reversed(lines) if "error" in line.lower()), lines[-1] if lines else ""
    )
    if any(hint in text for hint in GONE_HINTS):
        return PostFailed(f"Could not download: {last}"[:500])
    return TryLater(f"Download failed: {last}"[:500] or "Download failed with no output")


def _media_in(folder: Path) -> list[RawItem]:
    items = []
    for path in sorted(folder.iterdir()):
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            items.append(RawItem(path.name, "video"))
        elif suffix in IMAGE_EXTENSIONS:
            items.append(RawItem(path.name, "image"))
    return items
