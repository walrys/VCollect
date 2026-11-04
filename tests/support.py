"""Shared test set-ups that are more than a fixture."""

import time
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline.errors import PostFailed
from app.pipeline.post import Services
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services

PRIVATE = "Could not download: This account is private"


def wait_for_sync(client: TestClient, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        progress = client.get("/api/sync").json()
        if not progress["running"] and progress["finished_at"]:
            return progress
        time.sleep(0.05)
    raise AssertionError("Sync did not finish")


@dataclass
class SyncedApp:
    client: TestClient
    notion: FakeNotion
    services: Services
    pages: dict[str, str]  # caption -> Notion page id

    def post(self, caption: str) -> dict:
        return next(post for post in self.posts() if post["caption"] == caption)

    def posts(self) -> list[dict]:
        return self.client.get("/api/posts").json()["posts"]

    def search(self, text: str) -> list[int]:
        return self.client.get("/api/search", params={"q": text}).json()["ids"]


@contextmanager
def synced_app(settings, samples, schema: dict = FULL_SCHEMA):
    """An app after one Sync of three rows: two posts that work and a private profile, which
    fails and stays in the gallery as a placeholder."""
    notion = FakeNotion(schema)
    pages = {
        "pasta trick": notion.add_row(
            url="https://www.instagram.com/reel/AbCdEf/", caption="pasta trick", tags=["Italian"]
        ),
        "profile": notion.add_row(url="https://www.instagram.com/indiegamejoe"),
        "synth patch": notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="synth patch"),
    }
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["indiegamejoe"] = PostFailed(PRIVATE)
    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)
        yield SyncedApp(client, notion, services, pages)
