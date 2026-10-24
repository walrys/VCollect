"""The JSON API. The web page and the future iOS app use only these routes."""

from fastapi import APIRouter

from app.api import health

router = APIRouter(prefix="/api")
router.include_router(health.router)
