"""Stand-ins for the downloader, whisper and Ollama."""

import shutil
from collections.abc import Callable
from pathlib import Path

from app.pipeline.content import Download, PageCopy, RawItem
from app.pipeline.download import VIDEO_EXTENSIONS
from app.pipeline.post import Services
from app.pipeline.summarise import PostContext
from app.pipeline.urls import PostUrl


class FakeDownloader:
    """Copies sample files in as the post's media. `errors` maps a link fragment to the
    exception that link raises; `pages` maps one to what it brings back instead (see page())."""

    def __init__(self, files: list[Path], subtitles: str | None = None):
        self.files = files
        self.subtitles = subtitles
        self.errors: dict[str, Exception] = {}
        self.pages: dict[str, Callable[[Path], Download]] = {}
        self.calls: list[str] = []
        self.skips: list[dict[str, str]] = []  # the walls each call was told the run skips

    async def download(self, link: PostUrl, dest: Path, skip_sites=None) -> Download:
        self.calls.append(link.url)
        self.skips.append({site: str(wall) for site, wall in (skip_sites or {}).items()})
        for fragment, error in self.errors.items():
            if fragment in link.url:
                raise error
        dest.mkdir(parents=True, exist_ok=True)
        for fragment, make in self.pages.items():
            if fragment in link.url:
                return make(dest)
        return Download(
            items=_copy(self.files, dest),
            title="A title",
            uploader="someone",
            caption="the platform caption",
            subtitles=self.subtitles,
            thumbnail=None,
        )


def page(
    title: str,
    html: str = "",
    text: str = "",
    images: list[Path] = (),
    media: list[Path] = (),
    url: str | None = None,
) -> Callable[[Path], Download]:
    """A page download: its readable copy `html` names `images` as page/01.png, page/02.png ...
    in order (write {1}, {2} ... for them); `media` are its own media items, if any."""

    def make(dest: Path) -> Download:
        folder = dest / "page"
        folder.mkdir(parents=True, exist_ok=True)
        names = []
        for number, src in enumerate(images, start=1):
            name = f"page/{number:02}{src.suffix}"
            shutil.copyfile(src, dest / name)
            names.append(name)
        filled = html.format(None, *names) if names else html
        return Download(
            items=_copy(media, dest),
            title=title,
            uploader="a site",
            caption="the page's description",
            subtitles=None,
            thumbnail=None,
            page=PageCopy(html=filled, text=text, images=names),
            url=url,
        )

    return make


def _copy(files: list[Path], dest: Path) -> list[RawItem]:
    items = []
    for position, src in enumerate(files, start=1):
        target = dest / f"{position:02}{src.suffix}"
        shutil.copyfile(src, target)
        kind = "video" if src.suffix in VIDEO_EXTENSIONS else "image"
        items.append(RawItem(target.name, kind))
    return items


class FakeTranscriber:
    def __init__(self, text: str = "hello from the video"):
        self.text = text
        self.calls = 0

    async def transcribe(self, audio: Path) -> str:
        assert audio.stat().st_size > 0
        self.calls += 1
        return self.text


class FakeSummariser:
    """`before` runs ahead of each answer, so a test can raise or cancel mid-run."""

    def __init__(self, summary: str = "A short summary.", tags: list[str] | None = None):
        self.summary = summary
        self.tags = tags if tags is not None else ["cooking", "recipe"]
        self.before = None
        self.contexts: list[PostContext] = []

    async def summarise(self, post: PostContext) -> tuple[str, list[str]]:
        self.contexts.append(post)
        if self.before is not None:
            self.before()
        return self.summary, self.tags


def fake_services(files: list[Path], subtitles: str | None = None) -> Services:
    return Services(
        downloader=FakeDownloader(files, subtitles),
        transcriber=FakeTranscriber(),
        summariser=FakeSummariser(),
    )
