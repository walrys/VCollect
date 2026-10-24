"""The JSON API. The web page and the future iOS app use only these routes."""

from fastapi import APIRouter

from app.api import health, notion, sync

router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(notion.router)
router.include_router(sync.router)
