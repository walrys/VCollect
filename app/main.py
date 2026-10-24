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

WEB_DIR = Path(__file__).parent / "web"

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, notion: NotionClient | None = None) -> FastAPI:
    """`notion` replaces the client built from settings; tests pass a fake."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with closing(db.connect(settings.db_path)) as conn:
            db.migrate(conn)

        client = notion
        if client is None and settings.notion_configured:
            client = NotionClient(settings.notion_token, settings.notion_data_source_id)
        app.state.puller = None
        launch_pull = None
        if client is not None:
            app.state.puller = Puller(client, settings.notion_props, settings.db_path)
            launch_pull = asyncio.create_task(_pull_on_launch(app.state.puller))

        yield

        if launch_pull is not None:
            launch_pull.cancel()
        if client is not None:
            await client.aclose()

    app = FastAPI(title="VCollect", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(api.router)
    # Mounted last so /api routes match first.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


async def _pull_on_launch(puller: Puller) -> None:
    try:
        await puller.pull()
    except Exception as error:
        # puller.error keeps it for /api/health.
        logger.warning("Pull on launch failed: %s", error)
