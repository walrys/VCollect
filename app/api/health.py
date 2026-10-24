"""GET /api/health: what is configured, and which local AI services answer."""

import asyncio

from fastapi import APIRouter, Request
from httpx import AsyncClient, HTTPError

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    mirror = request.app.state.mirror
    async with AsyncClient(timeout=2) as client:
        ollama_models, whisper_up = await asyncio.gather(
            _ollama_models(client, settings.ollama_url),
            _answers(client, f"{settings.whisper_url}/health"),
        )
    return {
        "notion": {
            "configured": mirror is not None,
            "error": mirror.error if mirror else None,
        },
        "ollama": {
            "reachable": ollama_models is not None,
            "model": settings.ollama_model,
            "model_ready": settings.ollama_model in (ollama_models or []),
        },
        "whisper": {"reachable": whisper_up},
    }


async def _ollama_models(client: AsyncClient, base_url: str) -> list[str] | None:
    """Names of the models Ollama has pulled, or None when it does not answer."""
    try:
        response = await client.get(f"{base_url}/api/tags")
        response.raise_for_status()
    except HTTPError:
        return None
    return [model["name"] for model in response.json().get("models", [])]


async def _answers(client: AsyncClient, url: str) -> bool:
    try:
        response = await client.get(url)
    except HTTPError:
        return False
    return response.is_success
