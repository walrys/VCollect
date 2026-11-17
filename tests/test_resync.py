"""A post whose link changed since its media was fetched: flagged, and fetched afresh by Resync."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline.errors import PostFailed
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services
from tests.support import wait_for_sync

NEW_LINK = "https://www.instagram.com/reel/XyZ123/"


def change_link_in_notion(synced, caption: str, url: str) -> dict:
    synced.notion.edit_row(synced.pages[caption], url=url)
    synced.client.post("/api/sync")  # its pull brings the link in; nothing is new to process
    wait_for_sync(synced.client)
    return synced.post(caption)


def test_a_link_changed_in_notion_is_flagged(synced):
    post = change_link_in_notion(synced, "pasta trick", NEW_LINK)
    assert (post["url"], post["link_changed"]) == (NEW_LINK, True)
    assert (post["kind"], post["status"]) == ("media", "done")  # still on show
    assert synced.services.downloader.calls.count("https://www.instagram.com/p/XyZ123/") == 0


def test_resync_fetches_the_post_from_its_new_link(synced):
    post = change_link_in_notion(synced, "pasta trick", NEW_LINK)
    synced.services.summariser.summary = "About the new reel."

    answer = synced.client.post(f"/api/posts/{post['id']}/resync")
    assert answer.status_code == 200
    assert answer.json()["post"]["status"] == "new"  # back to Not started until it lands
    progress = wait_for_sync(synced.client)

    assert (progress["retry"], progress["total"], progress["done"]) == (True, 1, 1)
    after = synced.post("pasta trick")
    assert after["link_changed"] is False
    assert after["fetched_url"] == "https://www.instagram.com/p/XyZ123/"
    assert (after["status"], after["summary"]) == ("done", "About the new reel.")
    assert after["thumb"] != post["thumb"]  # a new version, past the browser's cache
    assert synced.services.downloader.calls[-1] == "https://www.instagram.com/p/XyZ123/"
    row = synced.notion.row(synced.pages["pasta trick"])
    assert (row["status"], row["summary"]) == ("Done", "About the new reel.")


def test_a_resync_that_fails_leaves_the_old_media_on_show(synced):
    post = change_link_in_notion(synced, "pasta trick", NEW_LINK)
    synced.services.downloader.errors["XyZ123"] = PostFailed("Could not download: gone")

    synced.client.post(f"/api/posts/{post['id']}/resync")
    progress = wait_for_sync(synced.client)

    assert progress["failed"] == 1
    after = synced.post("pasta trick")
    assert (after["kind"], after["status"], after["link_changed"]) == ("media", "failed", True)
    assert after["reason"] == "Could not download: gone"
    assert after["items"] == post["items"]
    rows = synced.client.get("/api/processing").json()["rows"]
    assert [(row["id"], row["stage"]) for row in rows if row["id"] == post["id"]] == [
        (post["id"], "failed")
    ]

    # Its Resync is offered still, and tries again.
    del synced.services.downloader.errors["XyZ123"]
    assert synced.client.post(f"/api/posts/{post['id']}/resync").status_code == 200
    wait_for_sync(synced.client)
    after = synced.post("pasta trick")
    assert (after["status"], after["link_changed"]) == ("done", False)


def test_only_a_changed_link_can_be_resynced(synced):
    post = synced.post("pasta trick")
    answer = synced.client.post(f"/api/posts/{post['id']}/resync")
    assert answer.status_code == 409
    assert synced.client.post("/api/posts/999/resync").status_code == 404


def test_a_changed_link_has_no_video_to_download(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    page_id = notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    services = fake_services([samples["portrait.mp4"]])
    services.downloader.previews["dvUGDMxl8OU"] = samples["photo.png"]
    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)
        notion.edit_row(page_id, url="https://youtu.be/AAAAAAAAAAA")
        client.post("/api/sync")
        wait_for_sync(client)
        (preview,) = client.get("/api/posts").json()["posts"]
        answer = client.post(f"/api/posts/{preview['id']}/video")

    assert (preview["kind"], preview["link_changed"]) == ("preview", True)
    assert answer.status_code == 409
