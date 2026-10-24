from contextlib import closing

from app import db
from tests.fakes.notion import FULL_SCHEMA, prop
from tests.support import synced_app, wait_for_sync


def waiting_edits(settings):
    with closing(db.connect(settings.db_path)) as conn:
        return [tuple(row) for row in conn.execute("SELECT post_id, field FROM pending_edits")]


def test_an_edit_saves_here_and_in_notion(synced, settings):
    post = synced.post("pasta trick")
    response = synced.client.patch(
        f"/api/posts/{post['id']}",
        json={
            "caption": "pasta trick, the sequel ",
            "summary": "A better summary.",
            "tags": ["Italian", " Weeknight   dinner "],
            "ai_tags": ["Pasta", "recipe"],
        },
    ).json()

    assert response["notion"] == "saved"
    saved = response["post"]
    assert saved["caption"] == "pasta trick, the sequel"
    assert saved["summary"] == "A better summary."
    assert saved["tags"] == ["Italian", "Weeknight dinner"]
    assert saved["ai_tags"] == ["pasta", "recipe"]  # AI tags stay lower-case

    row = synced.notion.row(synced.pages["pasta trick"])
    assert row["caption"] == "pasta trick, the sequel"
    assert row["summary"] == "A better summary."
    assert row["tags"] == ["Italian", "Weeknight dinner"]
    assert row["ai_tags"] == ["pasta", "recipe"]
    assert waiting_edits(settings) == []
    assert synced.search("sequel") == [post["id"]]


def test_only_changed_fields_are_sent(synced):
    post = synced.post("pasta trick")
    before = len(synced.notion.page_updates)

    unchanged = synced.client.patch(f"/api/posts/{post['id']}", json={"caption": "pasta trick"})
    assert unchanged.json()["notion"] == "unchanged"
    assert len(synced.notion.page_updates) == before

    synced.client.patch(f"/api/posts/{post['id']}", json={"caption": "x", "tags": ["Italian"]})
    assert list(synced.notion.page_updates[-1][1]) == ["Caption"]


def test_a_title_caption_is_written_as_the_title(settings, samples):
    with synced_app(settings, samples, {**FULL_SCHEMA, "Caption": prop("Caption", "title")}) as app:
        post = app.post("pasta trick")
        app.client.patch(f"/api/posts/{post['id']}", json={"caption": "renamed"})
        sent = app.notion.page_updates[-1][1]
    assert sent["Caption"] == {"title": [{"type": "text", "text": {"content": "renamed"}}]}


def test_an_edit_waits_while_notion_is_down(synced, settings):
    post = synced.post("synth patch")
    synced.notion.refuse_page_updates = True
    response = synced.client.patch(f"/api/posts/{post['id']}", json={"caption": "offline edit"})

    assert response.json()["notion"] == "waiting"
    assert response.json()["post"]["caption"] == "offline edit"
    assert waiting_edits(settings) == [(post["id"], "caption")]

    # The next pull (here the one Sync starts with) sends it before reading Notion back.
    synced.notion.refuse_page_updates = False
    synced.client.post("/api/sync")
    wait_for_sync(synced.client)
    assert synced.notion.row(synced.pages["synth patch"])["caption"] == "offline edit"
    assert synced.post("offline edit")["id"] == post["id"]
    assert waiting_edits(settings) == []


def test_tag_names_with_commas_are_refused(synced):
    post = synced.post("pasta trick")
    response = synced.client.patch(f"/api/posts/{post['id']}", json={"tags": ["a, b"]})
    assert response.status_code == 422
    assert "commas" in response.json()["detail"]
    assert synced.post("pasta trick")["tags"] == ["Italian"]


def test_promoting_copies_an_ai_tag_into_the_notion_tags(synced):
    post = synced.post("pasta trick")
    response = synced.client.patch(
        f"/api/posts/{post['id']}", json={"tags": [*post["tags"], "recipe"]}
    ).json()
    assert response["post"]["tags"] == ["Italian", "recipe"]
    assert response["post"]["ai_tags"] == ["cooking", "recipe"]


def test_editing_an_unknown_post_is_a_404(synced):
    assert synced.client.patch("/api/posts/9999", json={"caption": "x"}).status_code == 404


def test_delete_trashes_the_row_and_removes_the_files(synced, settings):
    post = synced.post("synth patch")
    media = settings.data_dir / "media" / str(post["id"])
    assert media.exists()

    response = synced.client.delete(f"/api/posts/{post['id']}")

    assert response.status_code == 200
    assert synced.notion.trashed == [synced.pages["synth patch"]]
    assert not media.exists()
    captions = [p["caption"] for p in synced.client.get("/api/posts").json()["posts"]]
    assert captions == ["pasta trick"]
    assert synced.search("synth") == []


def test_delete_keeps_everything_while_notion_is_down(synced, settings):
    post = synced.post("synth patch")
    synced.notion.refuse_page_updates = True

    response = synced.client.delete(f"/api/posts/{post['id']}")

    assert response.status_code == 502
    assert "nothing was deleted" in response.json()["detail"]
    assert (settings.data_dir / "media" / str(post["id"])).exists()
    assert synced.post("synth patch")["id"] == post["id"]


def test_add_link_creates_a_notion_row(synced):
    response = synced.client.post("/api/posts", json={"url": "https://youtu.be/bbbbbbbbbbb"})

    assert response.status_code == 201
    assert response.json()["new"] == 1
    rows = [synced.notion.row(page_id) for page_id in synced.notion._rows]
    added = next(row for row in rows if row.get("url") == "https://youtu.be/bbbbbbbbbbb")
    assert added["status"] == "Not started"


def test_add_link_warns_about_a_link_already_saved(synced):
    link = "https://www.instagram.com/p/AbCdEf/?igsh=xyz"
    response = synced.client.post("/api/posts", json={"url": link})
    assert response.status_code == 409
    assert response.json()["detail"]["duplicate"]["caption"] == "pasta trick"

    forced = synced.client.post("/api/posts", json={"url": link, "force": True})
    assert forced.status_code == 201


def test_add_link_refuses_what_is_not_a_post(synced):
    response = synced.client.post("/api/posts", json={"url": "https://www.instagram.com/someone"})
    assert response.status_code == 422
    assert "Not a single post" in response.json()["detail"]
