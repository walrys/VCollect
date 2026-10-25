import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.notion import mirror
from app.pipeline.errors import LoginNeeded, PostFailed
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services
from tests.support import wait_for_sync

LOGIN_WALL = "Instagram needs a login: add its cookies to cookies.txt in the data folder"


@pytest.fixture(autouse=True)
def pull_every_time(monkeypatch):
    monkeypatch.setattr(mirror, "REUSE_SECONDS", 0)


def sync(client: TestClient) -> dict:
    client.post("/api/sync")
    return wait_for_sync(client)


def rows(client: TestClient) -> list[dict]:
    return client.get("/api/processing").json()["rows"]


def test_processing_lists_every_row_sync_has_not_finished(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    notion.add_row(url="https://www.instagram.com/indiegamejoe")
    notion.add_row(url="https://www.instagram.com/reel/AAA/", caption="behind a wall")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["instagram.com"] = LoginNeeded("Instagram")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        progress = sync(client)
        notion.add_row(url="https://youtu.be/bbbbbbbbbbb")  # saved after that Sync
        client.post("/api/pull")
        result = client.get("/api/processing").json()

    assert progress["new"] == 0
    new, later, failed = result["rows"]  # newest saved first; Done rows are not listed
    assert (new["stage"], new["reason"], new["tried_at"]) == ("new", "", None)
    assert (later["stage"], later["caption"], later["reason"]) == (
        "later",
        "behind a wall",
        LOGIN_WALL,
    )
    assert failed["stage"] == "failed"
    assert failed["reason"].startswith("Not a single post")
    assert later["tried_at"] and failed["tried_at"]
    assert (result["done"], result["new"]) == (1, 1)


def test_a_row_left_for_later_is_retried_by_every_sync(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    page = notion.add_row(url="https://www.instagram.com/reel/AAA/")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["instagram.com"] = LoginNeeded("Instagram")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        sync(client)
        [row] = rows(client)
        # Only Failed rows are reset; this one is retried anyway.
        assert client.post("/api/processing/retry", json={"ids": [row["id"]]}).json() == {
            "reset": [],
            "new": 0,
        }

        del services.downloader.errors["instagram.com"]
        progress = sync(client)
        assert rows(client) == []

    assert (progress["total"], progress["done"]) == (1, 1)
    assert notion.row(page)["status"] == "Done"


def test_retry_sends_a_failed_row_back_to_sync_as_new(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    page = notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["dvUGDMxl8OU"] = PostFailed("Could not download: unavailable")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        sync(client)
        [row] = rows(client)
        assert (row["stage"], row["reason"]) == ("failed", "Could not download: unavailable")
        assert notion.row(page)["error"] == "Could not download: unavailable"

        del services.downloader.errors["dvUGDMxl8OU"]
        answer = client.post("/api/processing/retry", json={"ids": [row["id"]]}).json()
        assert answer == {"reset": [row["id"]], "new": 1}
        assert (notion.row(page)["status"], notion.row(page)["error"]) == ("Not started", "")
        assert rows(client)[0]["stage"] == "new"

        progress = sync(client)
        assert rows(client) == []

    assert progress["done"] == 1
    assert notion.row(page)["status"] == "Done"


def test_retry_now_processes_that_row_at_once_and_no_other(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    page = notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["dvUGDMxl8OU"] = PostFailed("Could not download: unavailable")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        sync(client)
        [row] = rows(client)
        notion.add_row(url="https://youtu.be/bbbbbbbbbbb")
        client.post("/api/pull")
        pulls = notion.query_calls

        del services.downloader.errors["dvUGDMxl8OU"]
        answer = client.post("/api/processing/retry", json={"ids": [row["id"]], "now": True})
        progress = wait_for_sync(client)
        [waiting] = rows(client)

    assert answer.json()["reset"] == [row["id"]]
    assert (progress["retry"], progress["total"], progress["done"]) == (True, 1, 1)
    assert notion.query_calls == pulls  # no pull: the row was just reset in Notion
    assert notion.row(page)["status"] == "Done"
    assert (waiting["url"], waiting["stage"]) == ("https://youtu.be/bbbbbbbbbbb", "new")


def test_retry_now_during_a_sync_is_processed_next(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    page = notion.add_row(url="https://youtu.be/fffffffffff")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["fffffffffff"] = PostFailed("Could not download: unavailable")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        sync(client)
        [row] = rows(client)
        del services.downloader.errors["fffffffffff"]
        notion.add_row(url="https://youtu.be/aaaaaaaaaaa")
        notion.add_row(url="https://youtu.be/bbbbbbbbbbb")  # newest, so the Sync takes it first
        retries = []

        def retry_once():  # pressed while the Sync works on its first row
            if not retries:
                retry = client.app.state.syncer.retry_now([row["id"]])
                retries.append(asyncio.get_running_loop().create_task(retry))

        services.summariser.before = retry_once
        progress = sync(client)

    assert [call[-11:] for call in services.downloader.calls[1:]] == [
        "bbbbbbbbbbb",
        "fffffffffff",
        "aaaaaaaaaaa",
    ]
    assert (progress["retry"], progress["total"], progress["done"]) == (False, 3, 3)
    assert notion.row(page)["status"] == "Done"


def test_retry_resets_nothing_when_notion_is_down(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    page = notion.add_row(url="https://www.instagram.com/indiegamejoe")
    services = fake_services([samples["small.mp4"]])

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        sync(client)
        [row] = rows(client)
        notion.refuse_page_updates = True
        response = client.post("/api/processing/retry", json={"ids": [row["id"]]})
        assert response.status_code == 502
        assert rows(client)[0]["stage"] == "failed"

    assert notion.row(page)["status"] == "Failed"


def test_processing_without_notion(client):
    assert client.get("/api/processing").json() == {"rows": [], "done": 0, "new": 0}
    assert client.post("/api/processing/retry", json={"ids": [1]}).status_code == 409
