from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline.download import INSTAGRAM_IMAGES
from app.pipeline.errors import LoginNeeded, PostFailed, TryLater
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services
from tests.support import wait_for_sync


def test_sync_needs_notion(client):
    assert client.post("/api/sync").status_code == 409
    assert client.get("/api/sync").json()["running"] is False


def test_sync_processes_every_new_row(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    done = notion.add_row(url="https://youtu.be/dvUGDMxl8OU")
    failed = notion.add_row(url="https://www.instagram.com/indiegamejoe")
    notion.add_row(url="https://youtu.be/aaaaaaaaaaa", status="Done")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["indiegamejoe"] = PostFailed("Could not download: private")

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
    assert any("Could not download: private" in problem for problem in progress["problems"])


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

    # Tried, so no longer new; the next Sync retries them all the same.
    assert (progress["done"], progress["later"], progress["new"]) == (1, 3, 0)
    assert len([call for call in services.downloader.calls if "instagram" in call]) == 1
    assert progress["problems"] == [
        "Instagram needs a login: add its cookies to cookies.txt in the data folder"
    ]


def test_skipped_posts_keep_the_walls_reason(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    for code in ("AAA", "BBB"):
        notion.add_row(url=f"https://www.instagram.com/reel/{code}/")
    services = fake_services([samples["small.mp4"]])
    reason = "Instagram is limiting anonymous requests: they wait for the next Sync"
    services.downloader.errors["instagram.com"] = LoginNeeded("Instagram", reason)

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)
        reasons = [row["reason"] for row in client.get("/api/processing").json()["rows"]]

    assert reasons == [reason, reason]


def test_the_downloader_hears_which_walls_the_run_met(settings, samples):
    # A post is only known as an image post once yt-dlp has read it, so the downloader has to
    # skip the image-post wall itself: the pipeline only knows the link's site, "Instagram".
    notion = FakeNotion(FULL_SCHEMA)
    for code in ("AAA", "BBB"):
        notion.add_row(url=f"https://www.instagram.com/p/{code}/")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["instagram.com"] = LoginNeeded(INSTAGRAM_IMAGES, "a reason")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)

    assert services.downloader.skips == [{}, {INSTAGRAM_IMAGES: "a reason"}]


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


def youtube_preview(settings, samples):
    """An app, its fakes, and a long YouTube video that one Sync has saved as a preview."""
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="a talk")
    services = fake_services([samples["small.mp4"]])
    services.downloader.previews["dvUGDMxl8OU"] = samples["photo.png"]
    return TestClient(create_app(settings, notion=notion, services=services)), services


def only_post(client) -> dict:
    [post] = client.get("/api/posts").json()["posts"]
    return post


def test_a_previews_video_downloads_at_once(settings, samples):
    client, services = youtube_preview(settings, samples)
    with client:
        client.post("/api/sync")
        wait_for_sync(client)
        preview = only_post(client)
        answer = client.post(f"/api/posts/{preview['id']}/video")
        progress = wait_for_sync(client)
        post = only_post(client)
        again = client.post(f"/api/posts/{post['id']}/video")

    assert (preview["kind"], preview["status"], preview["items"]) == ("preview", "done", [])
    assert (preview["duration"], preview["video_wanted"]) == (3600.0, False)
    assert preview["thumb"].startswith(f"/media/{preview['id']}/thumb.webp?v=")
    assert answer.json()["post"]["video_wanted"] is True
    assert (progress["video"], progress["retry"], progress["total"], progress["done"]) == (
        True,
        False,
        1,
        1,
    )
    assert (post["kind"], post["status"], post["video_wanted"]) == ("media", "done", False)
    assert [item["kind"] for item in post["items"]] == ["video"]
    assert again.status_code == 409  # a video already


def test_a_video_left_for_later_is_tried_again_by_the_next_sync(settings, samples):
    client, services = youtube_preview(settings, samples)
    with client:
        client.post("/api/sync")
        wait_for_sync(client)
        services.downloader.errors["dvUGDMxl8OU"] = TryLater("Download failed: timed out")
        client.post(f"/api/posts/{only_post(client)['id']}/video")
        first = wait_for_sync(client)
        waiting = only_post(client)

        del services.downloader.errors["dvUGDMxl8OU"]
        client.post("/api/sync")
        second = wait_for_sync(client)
        post = only_post(client)

    assert first["later"] == 1
    assert (waiting["kind"], waiting["video_wanted"]) == ("preview", True)
    assert waiting["reason"] == "Download failed: timed out"
    assert (second["total"], second["done"]) == (1, 1)
    assert (post["kind"], post["video_wanted"], post["reason"]) == ("media", False, "")


def test_a_wall_on_reels_does_not_stop_profiles(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://www.instagram.com/someone")
    notion.add_row(url="https://www.instagram.com/reel/AAA/")  # newest: processed first
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["/p/AAA/"] = LoginNeeded("Instagram")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        progress = wait_for_sync(client)

    assert (progress["done"], progress["later"]) == (1, 1)
    assert services.downloader.calls[-1] == "https://www.instagram.com/someone/"
