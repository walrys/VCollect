"""App factory, run with `uvicorn app.main:create_app --factory`."""

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, db
from app.config import Settings
from app.notion.client import NotionClient
from app.notion.mirror import Mirror
from app.pipeline.download import Downloader
from app.pipeline.post import Pipeline, Services
from app.pipeline.runner import Syncer
from app.pipeline.summarise import Summariser
from app.pipeline.transcribe import Transcriber

WEB_DIR = Path(__file__).parent / "web"

# The slim Python image has no system MIME table, and its built-in one lacks WebP.
mimetypes.add_type("image/webp", ".webp")

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    notion: NotionClient | None = None,
    services: Services | None = None,
) -> FastAPI:
    """`notion` and `services` replace the real ones built from settings; tests pass fakes."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        (settings.data_dir / "media").mkdir(parents=True, exist_ok=True)
        with closing(db.connect(settings.db_path)) as conn:
            db.migrate(conn)

        client = notion
        if client is None and settings.notion_configured:
            client = NotionClient(settings.notion_token, settings.notion_data_source_id)
        app.state.mirror = app.state.syncer = None
        launch_pull = None
        if client is not None:
            mirror = Mirror(client, settings.notion_props, settings.db_path)
            pipeline = Pipeline(
                services=services or _real_services(settings),
                notion=client,
                props=settings.notion_props,
                data_dir=settings.data_dir,
                keyframes=settings.keyframes,
            )
            app.state.mirror = mirror
            app.state.syncer = Syncer(pipeline, mirror, settings.db_path, settings.disk_cap_gb)
            launch_pull = asyncio.create_task(_pull_on_launch(mirror))

        yield

        if launch_pull is not None:
            launch_pull.cancel()
        if app.state.syncer is not None:
            await app.state.syncer.stop()
        if client is not None:
            await client.aclose()

    app = FastAPI(title="VCollect", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(api.router)
    # Videos, images and thumbnails; StaticFiles answers Range requests, so videos can seek.
    app.mount(
        "/media",
        StaticFiles(directory=settings.data_dir / "media", check_dir=False),
        name="media",
    )
    # Mounted last so /api and /media match first.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _real_services(settings: Settings) -> Services:
    return Services(
        downloader=Downloader(settings.cookies_path, settings.max_duration_min),
        transcriber=Transcriber(settings.whisper_url, settings.whisper_model),
        summariser=Summariser(settings.ollama_url, settings.ollama_model),
    )


async def _pull_on_launch(mirror: Mirror) -> None:
    try:
        await mirror.pull()
    except Exception as error:
        # mirror.error keeps it for /api/health.
        logger.warning("Pull on launch failed: %s", error)
