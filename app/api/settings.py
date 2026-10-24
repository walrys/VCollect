"""/api/settings: the menu's toggles, and how full the data folder is."""

import asyncio
from contextlib import closing

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app import db
from app.notion.mirror import AUTO_PULL, AUTO_PULL_SECONDS
from app.pipeline.runner import DISK_STOP_SHARE, disk_used

router = APIRouter()


class SettingsEdit(BaseModel):
    """Only the settings sent are changed."""

    auto_pull: bool | None = None


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    return await _settings(request)


@router.patch("/settings")
async def edit_settings(edit: SettingsEdit, request: Request) -> dict:
    if edit.auto_pull is not None:
        with closing(db.connect(request.app.state.settings.db_path)) as conn, conn:
            db.set_setting(conn, AUTO_PULL, "1" if edit.auto_pull else "0")
    return await _settings(request)


async def _settings(request: Request) -> dict:
    settings = request.app.state.settings
    with closing(db.connect(settings.db_path)) as conn:
        auto_pull = db.setting(conn, AUTO_PULL) == "1"
    cap = settings.disk_cap_gb * 1024**3
    return {
        "auto_pull": auto_pull,
        "auto_pull_minutes": AUTO_PULL_SECONDS // 60,
        "disk": {
            "used_bytes": await asyncio.to_thread(disk_used, settings.data_dir),
            "cap_bytes": int(cap),
            "sync_stops_at_bytes": int(cap * DISK_STOP_SHARE),
        },
    }
