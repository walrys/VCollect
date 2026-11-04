"""Stand-ins for the downloader, whisper and Ollama."""

import shutil
from pathlib import Path

from app.pipeline.download import VIDEO_EXTENSIONS, Download, RawItem
from app.pipeline.post import Services
from app.pipeline.summarise import PostContext
from app.pipeline.urls import PostUrl


class FakeDownloader:
    """Copies sample files in as the post's media. `errors` maps a link fragment to the
    exception that link raises."""

    def __init__(self, files: list[Path], subtitles: str | None = None):
        self.files = files
        self.subtitles = subtitles
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.skips: list[dict[str, str]] = []  # the sites each call was told the run skips

    async def download(self, link: PostUrl, dest: Path, skip_sites=None) -> Download:
        self.calls.append(link.url)
        self.skips.append(dict(skip_sites or {}))
        for fragment, error in self.errors.items():
            if fragment in link.url:
                raise error
        dest.mkdir(parents=True, exist_ok=True)
        items = []
        for position, src in enumerate(self.files, start=1):
            target = dest / f"{position:02}{src.suffix}"
            shutil.copyfile(src, target)
            kind = "video" if src.suffix in VIDEO_EXTENSIONS else "image"
            items.append(RawItem(target.name, kind))
        return Download(
            items=items,
            title="A title",
            uploader="someone",
            caption="the platform caption",
            subtitles=self.subtitles,
            thumbnail=None,
        )


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
