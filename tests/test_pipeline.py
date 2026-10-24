import asyncio

import pytest

from app.notion.pull import pull
from app.pipeline.errors import PostFailed, TryLater
from app.pipeline.post import Pipeline
from tests.fakes.notion import FULL_SCHEMA, PROPS, FakeNotion
from tests.fakes.services import fake_services


@pytest.fixture
def notion():
    return FakeNotion(FULL_SCHEMA)


def make_pipeline(notion, tmp_path, services):
    return Pipeline(services, notion, PROPS, tmp_path / "data", keyframes=4)


def add_post(notion, conn, **fields):
    page = notion.add_row(**fields)
    asyncio.run(pull(notion, PROPS, conn))
    post_id = conn.execute("SELECT id FROM posts WHERE notion_page_id = ?", (page,)).fetchone()[0]
    return page, post_id


def process(pipeline, conn, post_id):
    pipeline.index_links(conn)
    steps = []
    asyncio.run(pipeline.process(conn, post_id, steps.append))
    return steps


def post(conn, post_id):
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def items(conn, post_id):
    return conn.execute(
        "SELECT kind, file, width, height FROM items WHERE post_id = ? ORDER BY position",
        (post_id,),
    ).fetchall()


def ai_tags(conn, post_id):
    return [
        name
        for (name,) in conn.execute(
            "SELECT name FROM tags JOIN post_tags ON tag_id = tags.id"
            " WHERE post_id = ? AND kind = 'ai' ORDER BY name",
            (post_id,),
        )
    ]


def test_a_video_post_goes_all_the_way_to_done(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    page, post_id = add_post(notion, conn, url="https://youtu.be/dvUGDMxl8OU", caption="note")

    steps = process(pipeline, conn, post_id)

    assert steps == [
        "downloading",
        "compressing",
        "transcribing",
        "summarising",
        "saving to Notion",
    ]
    row = notion.row(page)
    assert (row["status"], row["summary"], row["ai_tags"]) == (
        "Done",
        "A short summary.",
        ["cooking", "recipe"],
    )
    saved = post(conn, post_id)
    assert (saved["status"], saved["summary"], saved["transcript"]) == (
        "Done",
        "A short summary.",
        "hello from the video",
    )
    assert (saved["title"], saved["uploader"]) == ("A title", "someone")
    assert [tuple(i) for i in items(conn, post_id)] == [("video", f"{post_id}/01.mp4", 720, 1280)]
    assert (tmp_path / "data" / "media" / saved["thumb_file"]).exists()
    assert not (tmp_path / "data" / "work" / str(post_id)).exists()
    assert ai_tags(conn, post_id) == ["cooking", "recipe"]

    context = services.summariser.contexts[0]
    assert len(context.images) == 4
    assert context.user_caption == "note"


def test_platform_subtitles_replace_whisper(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]], subtitles="from the captions")
    pipeline = make_pipeline(notion, tmp_path, services)
    _, post_id = add_post(notion, conn, url="https://youtu.be/dvUGDMxl8OU")

    process(pipeline, conn, post_id)
    assert post(conn, post_id)["transcript"] == "from the captions"
    assert services.transcriber.calls == 0


def test_a_silent_video_has_an_empty_transcript(notion, conn, tmp_path, samples):
    services = fake_services([samples["silent.mp4"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    _, post_id = add_post(notion, conn, url="https://youtu.be/dvUGDMxl8OU")

    process(pipeline, conn, post_id)
    assert post(conn, post_id)["transcript"] == ""
    assert services.transcriber.calls == 0


def test_an_image_carousel_keeps_every_image(notion, conn, tmp_path, samples):
    services = fake_services([samples["photo.png"], samples["photo.png"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    _, post_id = add_post(notion, conn, url="https://www.instagram.com/p/AbCdEf/")

    process(pipeline, conn, post_id)
    assert [i["kind"] for i in items(conn, post_id)] == ["image", "image"]
    assert all(i["file"].endswith(".webp") for i in items(conn, post_id))
    assert len(services.summariser.contexts[0].images) == 2


def test_a_rerun_resumes_where_the_last_one_stopped(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    page, post_id = add_post(notion, conn, url="https://youtu.be/dvUGDMxl8OU")

    def ollama_down():
        raise TryLater("Summary failed: Ollama is down")

    services.summariser.before = ollama_down
    with pytest.raises(TryLater):
        process(pipeline, conn, post_id)
    assert post(conn, post_id)["media_ready"] == 1
    assert notion.row(page).get("status") is None

    services.summariser.before = None
    steps = process(pipeline, conn, post_id)
    assert steps == ["summarising", "saving to Notion"]
    assert services.downloader.calls == ["https://www.youtube.com/watch?v=dvUGDMxl8OU"]
    assert services.transcriber.calls == 1


def test_notion_being_down_keeps_the_summary_for_next_time(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    page, post_id = add_post(notion, conn, url="https://youtu.be/dvUGDMxl8OU")

    notion.refuse_page_updates = True
    with pytest.raises(TryLater, match="Could not update Notion"):
        process(pipeline, conn, post_id)
    assert post(conn, post_id)["status"] == "Not started"

    notion.refuse_page_updates = False
    assert process(pipeline, conn, post_id) == ["saving to Notion"]
    assert len(services.summariser.contexts) == 1
    assert notion.row(page)["status"] == "Done"


def test_a_gone_post_is_marked_failed_in_notion(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]])
    services.downloader.errors["dvUGDMxl8OU"] = PostFailed("Could not download: Video unavailable")
    pipeline = make_pipeline(notion, tmp_path, services)
    page, post_id = add_post(notion, conn, url="https://youtu.be/dvUGDMxl8OU")

    with pytest.raises(PostFailed):
        process(pipeline, conn, post_id)
    assert (notion.row(page)["status"], notion.row(page)["error"]) == (
        "Failed",
        "Could not download: Video unavailable",
    )
    assert post(conn, post_id)["status"] == "Failed"
    assert not (tmp_path / "data" / "media" / str(post_id)).exists()


def test_a_profile_link_fails_without_downloading(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    page, post_id = add_post(notion, conn, url="https://www.instagram.com/indiegamejoe")

    with pytest.raises(PostFailed, match="Not a single post"):
        process(pipeline, conn, post_id)
    assert notion.row(page)["status"] == "Failed"
    assert services.downloader.calls == []


def test_the_later_copy_of_a_link_is_the_duplicate(notion, conn, tmp_path, samples):
    services = fake_services([samples["portrait.mp4"]])
    pipeline = make_pipeline(notion, tmp_path, services)
    first_page, first = add_post(notion, conn, url="https://www.instagram.com/reel/AbCdEf/")
    second_page, second = add_post(notion, conn, url="https://www.instagram.com/p/AbCdEf/?igsh=xyz")

    # Sync works newest first, so the later copy comes up before the original.
    with pytest.raises(PostFailed, match="Duplicate of https://app.notion.com/p/page-1"):
        process(pipeline, conn, second)
    process(pipeline, conn, first)

    assert notion.row(second_page)["status"] == "Failed"
    assert notion.row(first_page)["status"] == "Done"
