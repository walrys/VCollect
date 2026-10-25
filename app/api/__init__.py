"""The JSON API. The web page and the future iOS app use only these routes."""

from fastapi import APIRouter

from app.api import health, notion, posts, processing, settings, sync

router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(notion.router)
router.include_router(posts.router)
router.include_router(processing.router)
router.include_router(settings.router)
router.include_router(sync.router)
