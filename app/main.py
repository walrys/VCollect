"""App factory, run with `uvicorn app.main:create_app --factory`."""

from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, db
from app.config import Settings

WEB_DIR = Path(__file__).parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with closing(db.connect(settings.db_path)) as conn:
            db.migrate(conn)
        yield

    app = FastAPI(title="VCollect", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(api.router)
    # Mounted last so /api routes match first.
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
