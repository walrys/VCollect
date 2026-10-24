"""The Sync button: start a run, watch its progress, cancel it."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from app.pipeline.runner import Progress

router = APIRouter()


@router.get("/sync")
async def sync_status(request: Request) -> dict:
    syncer = request.app.state.syncer
    if syncer is None:
        return {**asdict(Progress()), "new": 0}
    return syncer.status()


@router.post("/sync")
async def start_sync(request: Request) -> dict:
    syncer = request.app.state.syncer
    if syncer is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    syncer.start()
    return syncer.status()


@router.post("/sync/cancel")
async def cancel_sync(request: Request) -> dict:
    syncer = request.app.state.syncer
    if syncer is not None:
        syncer.cancel()
        return syncer.status()
    return {**asdict(Progress()), "new": 0}
