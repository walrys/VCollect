"""Posts whose Notion rows were deleted: the choice before Sync, local-only posts, re-adding."""

import pytest

from app.notion import mirror


@pytest.fixture(autouse=True)
def every_pull_reads_notion(monkeypatch):
    monkeypatch.setattr(mirror, "REUSE_SECONDS", 0)


def delete_in_notion(synced, *captions):
    for caption in captions:
        synced.notion.delete_row(synced.pages[caption])
    synced.client.post("/api/pull")


def test_the_choice_deletes_ticked_posts_and_keeps_the_rest(synced, settings):
    pasta, synth = synced.post("pasta trick"), synced.post("synth patch")
    delete_in_notion(synced, "pasta trick", "synth patch")
    assert synced.post("pasta trick")["state"] == "deleted_in_notion"

    answer = synced.client.post(
        "/api/posts/deleted-in-notion", json={"delete": [pasta["id"]], "keep": [synth["id"]]}
    ).json()

    assert answer == {"deleted": [pasta["id"]], "kept": [synth["id"]]}
    posts = synced.client.get("/api/posts").json()["posts"]
    assert [(post["caption"], post["state"], post["notion_url"]) for post in posts] == [
        ("synth patch", "local_only", "")
    ]
    assert not (settings.data_dir / "media" / str(pasta["id"])).exists()
    assert synced.notion.trashed == []  # the rows had already left Notion


def test_the_choice_only_touches_posts_deleted_in_notion(synced):
    pasta = synced.post("pasta trick")
    answer = synced.client.post(
        "/api/posts/deleted-in-notion", json={"delete": [pasta["id"]], "keep": [9999]}
    ).json()
    assert answer == {"deleted": [], "kept": []}
    assert synced.post("pasta trick")["state"] == "synced"


def test_a_kept_post_stays_local_and_editable(synced):
    synth = synced.post("synth patch")
    delete_in_notion(synced, "synth patch")
    synced.client.post("/api/posts/deleted-in-notion", json={"keep": [synth["id"]]})
    synced.client.post("/api/pull")

    edit = synced.client.patch(f"/api/posts/{synth['id']}", json={"caption": "my synth"}).json()

    assert edit["notion"] == "local"
    assert edit["post"]["state"] == "local_only"
    assert synced.client.get("/api/sync").json()["new"] == 0


def test_deleting_a_post_that_left_notion_does_not_ask_notion(synced, settings):
    synth = synced.post("synth patch")
    delete_in_notion(synced, "synth patch")
    synced.notion.refuse_page_updates = True  # deleting must not need Notion

    assert synced.client.delete(f"/api/posts/{synth['id']}").status_code == 200
    assert synced.notion.trashed == []
    assert not (settings.data_dir / "media" / str(synth["id"])).exists()


def test_readd_gives_a_kept_post_a_fresh_row(synced):
    pasta = synced.post("pasta trick")
    delete_in_notion(synced, "pasta trick")
    synced.client.post("/api/posts/deleted-in-notion", json={"keep": [pasta["id"]]})

    answer = synced.client.post(f"/api/posts/{pasta['id']}/readd").json()

    post = answer["post"]
    assert post["state"] == "synced"
    page_id = post["notion_url"].rsplit("/", 1)[1]
    assert page_id != synced.pages["pasta trick"]
    row = synced.notion.row(page_id)
    assert row["url"] == "https://www.instagram.com/reel/AbCdEf/"
    assert (row["caption"], row["tags"], row["ai_tags"]) == (
        "pasta trick",
        ["Italian"],
        ["cooking", "recipe"],
    )
    assert (row["summary"], row["status"]) == ("A short summary.", "Done")
    assert answer["new"] == 0

    # Notion dates the new row today; the post keeps the date it was first saved.
    synced.client.post("/api/pull")
    assert synced.post("pasta trick")["saved_at"] == pasta["saved_at"]
    assert synced.notion.created_time(page_id) > pasta["saved_at"]


def test_only_local_only_posts_can_be_readded(synced):
    pasta = synced.post("pasta trick")
    assert synced.client.post(f"/api/posts/{pasta['id']}/readd").status_code == 409
