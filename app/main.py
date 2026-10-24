"""App factory, run with `uvicorn app.main:create_app --factory`."""

import asyncio
import logging
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, db
from app.config import Settings
from app.notion.client import NotionClient
from app.notion.pull import Puller
from app.pipeline.download import Downloader
from app.pipeline.post import Pipeline, Services
from app.pipeline.runner import Syncer
from app.pipeline.summarise import Summariser
from app.pipeline.transcribe import Transcriber

WEB_DIR = Path(__file__).parent / "web"

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
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with closing(db.connect(settings.db_path)) as conn:
            db.migrate(conn)

        client = notion
        if client is None and settings.notion_configured:
            client = NotionClient(settings.notion_token, settings.notion_data_source_id)
        app.state.puller = app.state.syncer = None
        launch_pull = None
        if client is not None:
            puller = Puller(client, settings.notion_props, settings.db_path)
            pipeline = Pipeline(
                services=services or _real_services(settings),
                notion=client,
                props=settings.notion_props,
                data_dir=settings.data_dir,
                keyframes=settings.keyframes,
            )
            app.state.puller = puller
            app.state.syncer = Syncer(pipeline, puller, settings.db_path, settings.disk_cap_gb)
            launch_pull = asyncio.create_task(_pull_on_launch(puller))

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
    # Mounted last so /api routes match first.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def _real_services(settings: Settings) -> Services:
    return Services(
        downloader=Downloader(settings.cookies_path, settings.max_duration_min),
        transcriber=Transcriber(settings.whisper_url, settings.whisper_model),
        summariser=Summariser(settings.ollama_url, settings.ollama_model),
    )


async def _pull_on_launch(puller: Puller) -> None:
    try:
        await puller.pull()
    except Exception as error:
        # puller.error keeps it for /api/health.
        logger.warning("Pull on launch failed: %s", error)
