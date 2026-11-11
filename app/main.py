"""App factory, run with `uvicorn app.main:create_app --factory`."""

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, db
from app.config import Settings
from app.notion.client import NotionClient
from app.notion.mirror import Mirror
from app.pipeline.runner import Processor, Syncer
from app.pipeline.worker import Worker

if TYPE_CHECKING:
    from app.pipeline.post import Services

WEB_DIR = Path(__file__).parent / "web"

# The slim Python image has no system MIME table, and its built-in one lacks WebP.
mimetypes.add_type("image/webp", ".webp")

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    notion: NotionClient | None = None,
    services: "Services | None" = None,
) -> FastAPI:
    """`notion` and `services` replace the real ones built from settings; tests pass fakes.
    Sync runs its posts in a worker process, or with fake services, in this one."""
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
        background: list[asyncio.Task] = []
        if client is not None:
            mirror = Mirror(client, settings.notion_props, settings.db_path)
            processor = _processor(settings, client, services)
            app.state.mirror = mirror
            app.state.syncer = Syncer(processor, mirror, settings.db_path, settings.disk_cap_gb)
            background += [
                asyncio.create_task(_pull_on_launch(mirror, settings.db_path)),
                asyncio.create_task(mirror.auto_pull()),
            ]

        yield

        for task in background:
            task.cancel()
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
    app.mount("/", PageFiles(directory=WEB_DIR, html=True), name="web")
    return app


class PageFiles(StaticFiles):
    """The page's files, which the browser checks for a newer copy on every load (a 304 while
    unchanged). Without a build step their names never change, so a browser left to guess how
    long they stay fresh keeps running old code against a newer API."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _processor(settings: Settings, client: NotionClient, services: "Services | None") -> Processor:
    if services is None:
        return Worker(settings.data_dir)
    # Imports the downloaders, which the server otherwise leaves to the worker.
    from app.pipeline.post import Pipeline

    return Pipeline(
        services=services,
        notion=client,
        props=settings.notion_props,
        data_dir=settings.data_dir,
        keyframes=settings.keyframes,
    )


async def _pull_on_launch(mirror: Mirror, db_path: Path) -> None:
    try:
        await mirror.pull()
    except Exception as error:
        # mirror.error keeps it for /api/health.
        logger.warning("Pull on launch failed: %s", error)
        return
    try:
        await _reset_wrong_kind(mirror, db_path)
    except Exception as error:
        logger.warning("Could not reset the rows that failed as the wrong kind of link: %s", error)


# What Sync said of links that were not a video or image post, before every link became one.
WRONG_KIND_ERRORS = (
    "Not a single post%",
    "No video in this post%",
    "Could not download: %Unsupported URL%",
)
WRONG_KIND_RESET = "wrong_kind_reset"  # app_settings key: "1" once those rows are reset


async def _reset_wrong_kind(mirror: Mirror, db_path: Path) -> None:
    """Once: sets the rows that failed only for being the wrong kind of link back to Not
    started, in Notion too, so the next Sync saves them as pages."""
    with closing(db.connect(db_path)) as conn:
        if db.setting(conn, WRONG_KIND_RESET) == "1":
            return
        failed = conn.execute(
            "SELECT id FROM posts WHERE status = 'Failed' AND notion_state = 'synced'"
            " AND (error LIKE ? OR error LIKE ? OR error LIKE ?)",
            WRONG_KIND_ERRORS,
        ).fetchall()
    await mirror.retry([post_id for (post_id,) in failed])
    with closing(db.connect(db_path)) as conn, conn:
        db.set_setting(conn, WRONG_KIND_RESET, "1")
