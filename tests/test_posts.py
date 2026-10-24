from contextlib import closing

import pytest

from app import db
from tests.fakes.notion import FULL_SCHEMA, FakeNotion


def captions(synced, ids):
    posts = {post["id"]: post for post in synced.client.get("/api/posts").json()["posts"]}
    return sorted(posts[post_id]["caption"] for post_id in ids)


def test_posts_list_the_ready_ones_newest_first(synced):
    posts = synced.client.get("/api/posts").json()["posts"]

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
def test_search_finds_word_prefixes_in_every_field(synced, text, expected):
    assert captions(synced, synced.search(text)) == expected


def test_an_empty_search_matches_nothing(synced):
    assert synced.search("  ") == []


def test_videos_can_seek(synced):
    src = synced.client.get("/api/posts").json()["posts"][0]["items"][0]["src"]
    response = synced.client.get(src, headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100


def test_thumbnails_are_served(synced):
    thumb = synced.client.get("/api/posts").json()["posts"][0]["thumb"]
    response = synced.client.get(thumb)
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
