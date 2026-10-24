import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline.errors import LoginNeeded
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services


def wait_for_sync(client, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        progress = client.get("/api/sync").json()
        if not progress["running"] and progress["finished_at"]:
            return progress
        time.sleep(0.05)
    raise AssertionError("Sync did not finish")


def test_sync_needs_notion(client):
    assert client.post("/api/sync").status_code == 409
    assert client.get("/api/sync").json()["running"] is False


def test_sync_processes_every_new_row(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    done = notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    failed = notion.add_row(url="https://www.instagram.com/indiegamejoe")
    notion.add_row(url="https://youtu.be/aaaaaaaaaaa", status="Done")
    services = fake_services([samples["small.mp4"]])

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        started = client.post("/api/sync").json()
        progress = wait_for_sync(client)

    assert started["running"] is True
    assert (progress["total"], progress["done"], progress["failed"], progress["later"]) == (
        2,
        1,
        1,
        0,
    )
    assert progress["new"] == 0
    assert notion.row(done)["status"] == "Done"
    assert notion.row(failed)["status"] == "Failed"
    assert any("Not a single post" in problem for problem in progress["problems"])


def test_a_login_wall_skips_the_rest_of_that_site(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    for code in ("AAA", "BBB", "CCC"):
        notion.add_row(url=f"https://www.instagram.com/reel/{code}/")
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["instagram.com"] = LoginNeeded("Instagram")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        progress = wait_for_sync(client)

    assert (progress["done"], progress["later"], progress["new"]) == (1, 3, 3)
    assert len([call for call in services.downloader.calls if "instagram" in call]) == 1
    assert progress["problems"] == [
        "Instagram needs a login: add its cookies to cookies.txt in the data folder"
    ]


def test_cancel_stops_at_the_next_step(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/aaaaaaaaaaa")
    notion.add_row(url="https://youtu.be/bbbbbbbbbbb")
    services = fake_services([samples["small.mp4"]])

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        services.summariser.before = lambda: client.app.state.syncer.cancel()
        client.post("/api/sync")
        progress = wait_for_sync(client)

    assert progress["stopped"] == "Cancelled"
    assert (progress["done"], progress["handled"], progress["new"]) == (0, 0, 2)
    assert len(services.summariser.contexts) == 1
