from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services
from tests.test_sync import wait_for_sync


@pytest.fixture
def gallery(settings, samples):
    """An app after one Sync of three rows: two posts that work and a profile link that fails."""
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(
        url="https://www.instagram.com/reel/AbCdEf/", caption="pasta trick", tags=["Italian"]
    )
    notion.add_row(url="https://www.instagram.com/indiegamejoe")
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="synth patch")
    services = fake_services([samples["small.mp4"]])
    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)
        yield client


def search(client, text):
    return client.get("/api/search", params={"q": text}).json()["ids"]


def captions(client, ids):
    posts = {post["id"]: post for post in client.get("/api/posts").json()["posts"]}
    return sorted(posts[post_id]["caption"] for post_id in ids)


def test_posts_list_the_ready_ones_newest_first(gallery):
    posts = gallery.get("/api/posts").json()["posts"]

    assert [post["caption"] for post in posts] == ["synth patch", "pasta trick"]
    pasta = posts[1]
    assert pasta["tags"] == ["Italian"]
    assert pasta["ai_tags"] == ["cooking", "recipe"]
    assert pasta["summary"] == "A short summary."
    assert pasta["state"] == "synced"
    assert pasta["notion_url"].startswith("https://app.notion.com/p/")
    assert pasta["thumb"] == f"/media/{pasta['id']}/thumb.webp"
    assert [(item["kind"], item["width"], item["height"]) for item in pasta["items"]] == [
        ("video", 480, 854)
    ]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("past", ["pasta trick"]),  # a word prefix of the caption
        ("ital", ["pasta trick"]),  # a Notion tag
        ("recip", ["pasta trick", "synth patch"]),  # an AI tag
        ("hello video", ["pasta trick", "synth patch"]),  # the transcript, every word
        ("summary", ["pasta trick", "synth patch"]),
        ("pasta synth", []),
        ('"pasta" (trick*', ["pasta trick"]),  # search syntax is just text
    ],
)
def test_search_finds_word_prefixes_in_every_field(gallery, text, expected):
    assert captions(gallery, search(gallery, text)) == expected


def test_an_empty_search_matches_nothing(gallery):
    assert search(gallery, "  ") == []


def test_videos_can_seek(gallery):
    src = gallery.get("/api/posts").json()["posts"][0]["items"][0]["src"]
    response = gallery.get(src, headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100


def test_thumbnails_are_served(gallery):
    thumb = gallery.get("/api/posts").json()["posts"][0]["thumb"]
    response = gallery.get(thumb)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


def test_a_pull_keeps_search_in_step_with_notion(settings, conn):
    import asyncio

    from app.notion.pull import pull
    from tests.fakes.notion import PROPS

    notion = FakeNotion(FULL_SCHEMA)
    page = notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="old words")
    asyncio.run(pull(notion, PROPS, conn))
    assert len(db.search(conn, "old")) == 1

    notion.edit_row(page, caption="new words")
    asyncio.run(pull(notion, PROPS, conn))
    assert db.search(conn, "old") == []
    assert len(db.search(conn, "new")) == 1


def test_migrating_indexes_existing_posts(tmp_path):
    with closing(db.connect(tmp_path / "old.db")) as conn:
        conn.executescript(f"BEGIN; {db.MIGRATIONS[0]} {db.MIGRATIONS[1]} {db.MIGRATIONS[2]}"
                           " PRAGMA user_version = 3; COMMIT;")  # fmt: skip
        conn.execute("INSERT INTO posts (url, user_caption, saved_at) VALUES ('x', 'kept', 'now')")
        conn.commit()
        db.migrate(conn)
        assert len(db.search(conn, "kept")) == 1
