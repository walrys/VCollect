import asyncio
import json

import httpx
import pytest

from app.notion import client as notion_client
from app.notion.client import NotionClient, NotionError


def run(handler, call):
    async def go():
        client = NotionClient("secret", "source-1", transport=httpx.MockTransport(handler))
        try:
            return await call(client), client
        finally:
            await client.aclose()

    return asyncio.run(go())


def page(page_id: str) -> dict:
    return {"object": "page", "id": page_id}


def test_requests_carry_the_token_and_api_version():
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(200, json={"id": "source-1", "properties": {}})

    run(handler, lambda client: client.retrieve_data_source())
    assert seen[0]["Authorization"] == "Bearer secret"
    assert seen[0]["Notion-Version"] == "2026-03-11"


def test_query_follows_cursors_to_the_last_page():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "start_cursor" not in body:
            return httpx.Response(
                200, json={"results": [page("a")], "has_more": True, "next_cursor": "c2"}
            )
        return httpx.Response(
            200, json={"results": [page("b")], "has_more": False, "next_cursor": None}
        )

    (pages, complete), _ = run(handler, lambda client: client.query_all_pages())
    assert [p["id"] for p in pages] == ["a", "b"]
    assert complete
    assert bodies[1]["start_cursor"] == "c2"
    assert bodies[0]["page_size"] == 100


def test_a_capped_query_is_reported_incomplete():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [page("a")],
                "has_more": False,
                "next_cursor": None,
                "request_status": {
                    "type": "incomplete",
                    "incomplete_reason": "query_result_limit_reached",
                },
            },
        )

    (_, complete), _ = run(handler, lambda client: client.query_all_pages())
    assert not complete


def test_rate_limited_requests_are_retried(monkeypatch):
    monkeypatch.setattr(notion_client, "RETRY_BASE_SECONDS", 0)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "rate_limited"}),
            httpx.Response(503, json={"code": "service_unavailable"}),
            httpx.Response(200, json={"id": "source-1", "properties": {}}),
        ]
    )

    source, _ = run(lambda request: next(responses), lambda client: client.retrieve_data_source())
    assert source["id"] == "source-1"


def test_a_database_id_resolves_to_its_only_data_source():
    def handler(request):
        if request.url.path == "/v1/data_sources/source-1":
            return httpx.Response(404, json={"code": "object_not_found", "message": "nope"})
        if request.url.path == "/v1/databases/source-1":
            return httpx.Response(200, json={"data_sources": [{"id": "real", "name": "Saved"}]})
        if request.url.path == "/v1/data_sources/real":
            return httpx.Response(200, json={"id": "real", "properties": {}})
        return httpx.Response(500)

    source, client = run(handler, lambda client: client.retrieve_data_source())
    assert source["id"] == "real"
    assert client.data_source_id == "real"


def test_an_unknown_id_keeps_the_original_not_found_error():
    def handler(request):
        return httpx.Response(404, json={"code": "object_not_found", "message": "nope"})

    with pytest.raises(NotionError) as caught:
        run(handler, lambda client: client.retrieve_data_source())
    assert caught.value.status == 404
    assert caught.value.code == "object_not_found"
