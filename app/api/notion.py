"""POST /api/pull: bring Notion's rows into the gallery."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from httpx import HTTPError

from app.notion.client import NotionError
from app.notion.schema import SchemaError

router = APIRouter()


@router.post("/pull")
async def pull(request: Request) -> dict:
    puller = request.app.state.puller
    if puller is None:
        raise HTTPException(
            409, "Notion is not configured: set NOTION_TOKEN and NOTION_DATA_SOURCE_ID."
        )
    try:
        result = await puller.pull()
    except SchemaError as error:
        raise HTTPException(409, str(error)) from error
    except (NotionError, HTTPError) as error:
        raise HTTPException(502, f"Could not pull from Notion: {error}") from error
    return asdict(result)
