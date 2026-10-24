"""Thin async client for the Notion API calls VCollect makes."""

import asyncio

import httpx

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
PAGE_SIZE = 100
MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 0.5


class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} {code}: {message}")
        self.status = status
        self.code = code


class NotionClient:
    def __init__(
        self,
        token: str,
        data_source_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.data_source_id = data_source_id
        self._http = httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
            timeout=30,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def retrieve_data_source(self) -> dict:
        """The data source's schema.

        Also accepts the ID of a database with a single data source, which is the ID a
        Notion URL shows; `data_source_id` is then replaced with the real one.
        """
        try:
            return await self._request("GET", f"/data_sources/{self.data_source_id}")
        except NotionError as error:
            if error.status != 404:
                raise
            self.data_source_id = await self._only_data_source_of(self.data_source_id, error)
        return await self._request("GET", f"/data_sources/{self.data_source_id}")

    async def update_data_source_properties(self, properties: dict) -> dict:
        return await self._request(
            "PATCH", f"/data_sources/{self.data_source_id}", {"properties": properties}
        )

    async def update_page(self, page_id: str, properties: dict) -> dict:
        return await self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    async def query_all_pages(self) -> tuple[list[dict], bool]:
        """Every row, plus whether the set is complete (Notion caps a query at 10,000)."""
        pages: list[dict] = []
        complete = True
        body: dict = {"page_size": PAGE_SIZE, "result_type": "page"}
        while True:
            response = await self._request(
                "POST", f"/data_sources/{self.data_source_id}/query", body
            )
            pages.extend(result for result in response["results"] if result["object"] == "page")
            if response.get("request_status", {}).get("type") == "incomplete":
                complete = False
            if not response.get("has_more"):
                return pages, complete
            body = {**body, "start_cursor": response["next_cursor"]}

    async def _only_data_source_of(self, database_id: str, not_found: NotionError) -> str:
        try:
            database = await self._request("GET", f"/databases/{database_id}")
        except NotionError:
            raise not_found from None
        sources = database.get("data_sources", [])
        if len(sources) != 1:
            raise NotionError(
                400,
                "ambiguous_database",
                f"the database has {len(sources)} data sources; "
                "set NOTION_DATA_SOURCE_ID to the one to use",
            )
        return sources[0]["id"]

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        attempt = 1
        while True:
            response = await self._http.request(method, path, json=body)
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(_retry_delay(response, attempt))
                attempt += 1
                continue
            if response.is_success:
                return response.json()
            raise _error(response)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return float(retry_after)
    return RETRY_BASE_SECONDS * 2 ** (attempt - 1)


def _error(response: httpx.Response) -> NotionError:
    try:
        body = response.json()
    except ValueError:
        return NotionError(response.status_code, "unknown", response.text[:200])
    return NotionError(response.status_code, body.get("code", "unknown"), body.get("message", ""))
