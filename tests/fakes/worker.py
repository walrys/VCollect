"""Sync's worker with fake services: `python -m tests.fakes.worker <setup>`. It is the real
worker (app.pipeline.worker.main) on real pipes; only the downloader, whisper, Ollama and
Notion are fakes.

`setup` is JSON: {"files": [sample paths], "errors": {link fragment: [error class, *args]}}.
A link holding "slow_down" takes a second to download; one holding "worker_dies" kills the
worker. Each worker leaves a file named after its pid in DATA_DIR/workers."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from app.config import Settings
from app.pipeline import errors
from app.pipeline.post import Pipeline
from app.pipeline.worker import main
from tests.fakes.services import FakeDownloader, fake_services


class AnyPage:
    """Notion as a worker meets it: every page update lands."""

    async def update_page(self, page_id: str, properties: dict) -> dict:
        return {}

    async def aclose(self) -> None:
        pass


class Downloader(FakeDownloader):
    async def download(self, link, dest, skip_sites=None, video=False):
        if "worker_dies" in link.url:
            os.kill(os.getpid(), signal.SIGKILL)
        if "slow_down" in link.url:
            await asyncio.sleep(1)
        return await super().download(link, dest, skip_sites, video)


def fake_pipeline(settings: Settings) -> Pipeline:
    setup = json.loads(sys.argv[1])
    workers = settings.data_dir / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    (workers / str(os.getpid())).touch()

    services = fake_services([])
    services.downloader = Downloader([Path(file) for file in setup["files"]])
    for fragment, (kind, *args) in setup.get("errors", {}).items():
        services.downloader.errors[fragment] = getattr(errors, kind)(*args)
    return Pipeline(services, AnyPage(), settings.notion_props, settings.data_dir, keyframes=4)


if __name__ == "__main__":
    main(fake_pipeline)
