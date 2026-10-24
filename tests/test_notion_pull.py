import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.notion.pull import pull
from tests.fakes.notion import EXISTING_SCHEMA, FULL_SCHEMA, PROPS, FakeNotion, prop


def pull_once(fake, conn):
    return asyncio.run(pull(fake, PROPS, conn))


def post(conn, page_id):
    return conn.execute("SELECT * FROM posts WHERE notion_page_id = ?", (page_id,)).fetchone()


def tags(conn, page_id, kind):
    rows = conn.execute(
        "SELECT tags.name FROM tags"
        " JOIN post_tags ON post_tags.tag_id = tags.id"
        " JOIN posts ON posts.id = post_tags.post_id"
        " WHERE posts.notion_page_id = ? AND tags.kind = ? ORDER BY tags.name",
        (page_id, kind),
    )
    return [name for (name,) in rows]


def test_rows_become_posts(conn):
    fake = FakeNotion(FULL_SCHEMA)
    reel = fake.add_row(
        url="https://www.instagram.com/reel/abc/",
        caption="pasta trick",
        tags=["Cooking", "Italian"],
    )
    fake.add_row(url="https://youtu.be/xyz", status="Done", summary="Two sentences.", ai_tags=["x"])

    result = pull_once(fake, conn)

    assert (result.pulled, result.new, result.deleted_in_notion) == (2, 1, 0)
    row = post(conn, reel)
    assert row["url"] == "https://www.instagram.com/reel/abc/"
    assert row["user_caption"] == "pasta trick"
    assert row["status"] == "Not started"
    assert row["saved_at"] == fake.created_time(reel)
    assert tags(conn, reel, "notion") == ["Cooking", "Italian"]


def test_a_title_caption_is_read(conn):
    fake = FakeNotion({**FULL_SCHEMA, "Caption": prop("Caption", "title")})
    page = fake.add_row(url="https://youtu.be/a", caption="VoiceStudio Local Elevenlabs")
    pull_once(fake, conn)

    assert post(conn, page)["user_caption"] == "VoiceStudio Local Elevenlabs"


def test_notion_tags_and_ai_tags_stay_separate(conn):
    fake = FakeNotion(FULL_SCHEMA)
    page = fake.add_row(url="https://youtu.be/a", tags=["travel"], ai_tags=["travel", "beach"])
    pull_once(fake, conn)

    assert tags(conn, page, "notion") == ["travel"]
    assert tags(conn, page, "ai") == ["beach", "travel"]


def test_edits_in_notion_update_the_post(conn):
    fake = FakeNotion(FULL_SCHEMA)
    page = fake.add_row(url="https://youtu.be/a", caption="old", tags=["one", "two"])
    pull_once(fake, conn)

    fake.edit_row(page, caption="new", tags=["two", "three"], status="Failed", error="private")
    result = pull_once(fake, conn)

    row = post(conn, page)
    assert (row["user_caption"], row["status"], row["error"]) == ("new", "Failed", "private")
    assert tags(conn, page, "notion") == ["three", "two"]
    assert result.new == 0


def test_rows_removed_from_notion_are_flagged_until_restored(conn):
    fake = FakeNotion(FULL_SCHEMA)
    kept = fake.add_row(url="https://youtu.be/a")
    removed = fake.add_row(url="https://youtu.be/b")
    pull_once(fake, conn)

    row = fake.delete_row(removed)
    result = pull_once(fake, conn)
    assert result.deleted_in_notion == 1
    assert post(conn, removed)["notion_state"] == "deleted_in_notion"
    assert post(conn, kept)["notion_state"] == "synced"

    fake.restore_row(removed, row)
    result = pull_once(fake, conn)
    assert result.deleted_in_notion == 0
    assert post(conn, removed)["notion_state"] == "synced"


def test_a_capped_pull_flags_nothing(conn):
    fake = FakeNotion(FULL_SCHEMA)
    page = fake.add_row(url="https://youtu.be/a")
    pull_once(fake, conn)

    fake.delete_row(page)
    fake.capped = True
    pull_once(fake, conn)
    assert post(conn, page)["notion_state"] == "synced"


def test_rows_without_a_status_yet_count_as_new(conn):
    fake = FakeNotion(EXISTING_SCHEMA)
    page = fake.add_row(url="https://youtu.be/a")

    result = pull_once(fake, conn)
    assert result.new == 1
    assert post(conn, page)["status"] == "Not started"


def test_pull_endpoint_needs_notion_configured(client):
    response = client.post("/api/pull")
    assert response.status_code == 409
    assert "NOTION_TOKEN" in response.json()["detail"]


def test_pull_endpoint_sets_up_the_schema_and_reuses_a_recent_pull(settings):
    fake = FakeNotion(EXISTING_SCHEMA)
    fake.add_row(url="https://youtu.be/a")

    with TestClient(create_app(settings, notion=fake)) as client:
        first = client.post("/api/pull")
        second = client.post("/api/pull")

    assert first.status_code == 200
    assert (first.json()["pulled"], first.json()["new"]) == (1, 1)
    assert second.json() == first.json()
    assert fake.query_calls == 1
    assert "Status" in fake.properties


def test_pull_endpoint_reports_schema_problems_in_health(settings):
    fake = FakeNotion({"Name": EXISTING_SCHEMA["Name"]})

    with TestClient(create_app(settings, notion=fake)) as client:
        response = client.post("/api/pull")
        health = client.get("/api/health").json()

    assert response.status_code == 409
    assert 'no "URL" URL property' in response.json()["detail"]
    assert health["notion"]["configured"] is True
    assert 'no "URL" URL property' in health["notion"]["error"]
