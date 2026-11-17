import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.pipeline.errors import LoginNeeded, PostNeedsLogin, TryLater
from app.pipeline.urls import INSTAGRAM_API
from tests.fakes.notion import FULL_SCHEMA, FakeNotion
from tests.fakes.services import fake_services, page
from tests.support import PRIVATE, wait_for_sync


def captions(synced, ids):
    posts = {post["id"]: post for post in synced.posts()}
    return sorted(posts[post_id]["caption"] for post_id in ids)


def test_posts_list_every_row_newest_first(synced):
    posts = synced.posts()

    assert [post["caption"] for post in posts] == ["synth patch", "", "pasta trick"]
    pasta = posts[2]
    assert pasta["tags"] == ["Italian"]
    assert pasta["ai_tags"] == ["cooking", "recipe"]
    assert pasta["summary"] == "A short summary."
    assert (pasta["state"], pasta["status"], pasta["kind"]) == ("synced", "done", "media")
    assert pasta["platform"] == "instagram"
    assert pasta["notion_url"].startswith("https://app.notion.com/p/")
    # Versioned by the link the media came from, so a resync's files are never stale copies.
    assert pasta["thumb"].startswith(f"/media/{pasta['id']}/thumb.webp?v=")
    assert pasta["fetched_url"] == "https://www.instagram.com/p/AbCdEf/"
    assert pasta["link_changed"] is False
    assert [(item["kind"], item["width"], item["height"]) for item in pasta["items"]] == [
        ("video", 480, 854)
    ]


def test_a_failed_row_is_a_placeholder_with_its_reason(synced):
    profile = synced.posts()[1]
    assert (profile["kind"], profile["status"], profile["reason"]) == (
        "placeholder",
        "failed",
        PRIVATE,
    )
    assert (profile["thumb"], profile["page"], profile["items"]) == (None, None, [])
    assert profile["url"] == "https://www.instagram.com/indiegamejoe"


def test_rows_waiting_for_sync_say_why(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://www.reddit.com/r/gamedev/s/Z92cYYKKPT", caption="wall")
    notion.add_row(url="https://www.instagram.com/reel/AGE/", caption="age")
    notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="ollama down")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["reddit.com"] = LoginNeeded("reddit.com")
    services.downloader.errors["AGE"] = PostNeedsLogin("Age-restricted")
    services.downloader.errors["dvUGDMxl8OU"] = TryLater("Download failed: timed out")

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/pull")
        before = {
            post["caption"]: post["status"] for post in client.get("/api/posts").json()["posts"]
        }
        client.post("/api/sync")
        wait_for_sync(client)
        posts = {post["caption"]: post for post in client.get("/api/posts").json()["posts"]}

    assert before == {"wall": "new", "age": "new", "ollama down": "new"}
    assert {caption: post["status"] for caption, post in posts.items()} == {
        "wall": "login",
        "age": "login",
        "ollama down": "waiting",
    }
    assert posts["wall"]["platform"] == "reddit"
    assert posts["ollama down"]["reason"] == "Download failed: timed out"


def test_a_rate_limit_is_waiting_not_a_login(settings, samples):
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://www.instagram.com/someone", caption="limited")
    services = fake_services([samples["small.mp4"]])
    services.downloader.errors["someone"] = LoginNeeded(INSTAGRAM_API, "limited", False)

    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)
        [post] = client.get("/api/posts").json()["posts"]

    assert (post["status"], post["reason"]) == ("waiting", "limited")


@pytest.fixture
def with_page(settings, samples):
    """An app after a Sync of one page: a readable copy holding one image."""
    notion = FakeNotion(FULL_SCHEMA)
    notion.add_row(url="https://example.com/notes/sourdough", caption="bread notes")
    services = fake_services([samples["small.mp4"]])
    services.downloader.pages["example.com"] = page(
        "How to keep a starter",
        html='<article class="reader"><p>Feed it daily with rye.</p><img src="{1}"></article>',
        text="How to keep a starter\nFeed it daily with rye.",
        images=[samples["photo.png"]],
    )
    with TestClient(create_app(settings, notion=notion, services=services)) as client:
        client.post("/api/sync")
        wait_for_sync(client)
        yield client


def test_a_page_post_has_a_readable_copy_and_no_thumbnail(with_page, settings):
    [post] = with_page.get("/api/posts").json()["posts"]

    assert (post["kind"], post["status"], post["thumb"]) == ("page", "done", None)
    assert post["title"] == "How to keep a starter"
    assert post["description"] == "the page's description"
    html = with_page.get(post["page"]).json()["html"]
    assert "Feed it daily" in html
    image = f"/media/{post['id']}/page-01.webp"
    assert f'src="{image}"' in html
    assert with_page.get(image).headers["content-type"] == "image/webp"


@pytest.mark.parametrize("text", ["rye", "sourdough", "example"])
def test_search_finds_the_page_and_its_link(with_page, text):
    assert len(with_page.get("/api/search", params={"q": text}).json()["ids"]) == 1


def test_a_media_post_has_no_readable_copy(synced):
    pasta = synced.post("pasta trick")
    assert pasta["page"] is None
    assert synced.client.get(f"/api/posts/{pasta['id']}/page").status_code == 404


def test_cookie_status_reads_cookies_txt_for_the_posts_site(synced, settings):
    profile = synced.posts()[1]

    def status():
        return synced.client.get(f"/api/posts/{profile['id']}/cookies").json()

    assert status() == {"domain": "instagram.com", "state": "missing", "expires": None}

    past, future = int(time.time()) - 86400, int(time.time()) + 86400
    lines = ["# Netscape HTTP Cookie File", f".instagram.com\tTRUE\t/\tTRUE\t{past}\tsessionid\tx"]
    settings.cookies_path.write_text("\n".join(lines) + "\n")
    assert status()["state"] == "expired"

    lines.append(f".instagram.com\tTRUE\t/\tTRUE\t{future}\tcsrftoken\ty")
    settings.cookies_path.write_text("\n".join(lines) + "\n")
    answer = status()
    assert (answer["state"], answer["expires"][:4]) == (
        "present",
        time.strftime("%Y", time.gmtime(future)),
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("past", ["pasta trick"]),  # a word prefix of the caption
        ("ital", ["pasta trick"]),  # a Notion tag
        ("recip", ["pasta trick", "synth patch"]),  # an AI tag
        ("hello video", ["pasta trick", "synth patch"]),  # the transcript, every word
        ("summary", ["pasta trick", "synth patch"]),
        ("indiegamejoe", [""]),  # a word of the link: the profile has no caption
        ("youtu", ["synth patch"]),
        ("pasta synth", []),
        ('"pasta" (trick*', ["pasta trick"]),  # search syntax is just text
    ],
)
def test_search_finds_word_prefixes_in_every_field(synced, text, expected):
    assert captions(synced, synced.search(text)) == expected


def test_an_empty_search_matches_nothing(synced):
    assert synced.search("  ") == []


def test_videos_can_seek(synced):
    src = synced.posts()[0]["items"][0]["src"]
    response = synced.client.get(src, headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100


def test_thumbnails_are_served(synced):
    thumb = synced.posts()[0]["thumb"]
    response = synced.client.get(thumb)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"


def test_a_pull_keeps_search_in_step_with_notion(settings, conn):
    import asyncio

    from app.notion.pull import pull
    from tests.fakes.notion import PROPS

    notion = FakeNotion(FULL_SCHEMA)
    page_id = notion.add_row(url="https://youtu.be/dvUGDMxl8OU", caption="old words")
    asyncio.run(pull(notion, PROPS, conn))
    assert len(db.search(conn, "old")) == 1

    notion.edit_row(page_id, caption="new words")
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


def test_migrating_marks_the_rows_a_login_would_let_through(tmp_path):
    with closing(db.connect(tmp_path / "old.db")) as conn:
        for sql in db.MIGRATIONS[:6]:
            conn.executescript(sql)
        conn.execute("PRAGMA user_version = 6")
        conn.executemany(
            "INSERT INTO posts (url, saved_at, retry_error, normalized_url)"
            " VALUES (?, 'now', ?, ?)",
            [
                (
                    "https://www.reddit.com/r/a/s/b",
                    "reddit.com needs a login: add its cookies",
                    "x",
                ),
                (
                    "https://www.instagram.com/reel/A/",
                    "ERROR: This content isn't available to everyone",
                    "y",
                ),
                ("https://youtu.be/dvUGDMxl8OU", "Download failed: timed out", "z"),
            ],
        )
        conn.commit()
        db.migrate(conn)
        rows = conn.execute("SELECT needs_login, normalized_url FROM posts ORDER BY id").fetchall()
    assert [tuple(row) for row in rows] == [(1, None), (1, None), (0, None)]


def test_migrating_remembers_the_link_media_came_from(tmp_path):
    with closing(db.connect(tmp_path / "old.db")) as conn:
        for sql in db.MIGRATIONS[:8]:
            conn.executescript(sql)
        conn.execute("PRAGMA user_version = 8")
        conn.executemany(
            "INSERT INTO posts (url, saved_at, media_ready, normalized_url, platform)"
            " VALUES (?, 'now', ?, ?, ?)",
            [
                # Its link was changed in Notion since: the key is still the first link's.
                (
                    "https://www.instagram.com/reel/New/",
                    1,
                    "https://www.instagram.com/p/Old/",
                    "instagram",
                ),
                # A share link, resolved by its download.
                (
                    "https://www.reddit.com/r/x/s/Shared1",
                    1,
                    "https://www.reddit.com/comments/abc/",
                    "reddit",
                ),
                ("https://youtu.be/dvUGDMxl8OU", 0, "https://www.youtube.com/watch?v=x", None),
            ],
        )
        conn.execute(
            "INSERT INTO pending_edits (post_id, field, edited_at) VALUES (1, 'caption', 'now')"
        )
        conn.commit()
        db.migrate(conn)
        rows = conn.execute(
            "SELECT fetched_url, normalized_url, platform FROM posts ORDER BY id"
        ).fetchall()
        edits = conn.execute("SELECT post_id, field FROM pending_edits").fetchall()
        conn.execute(
            "INSERT INTO pending_edits (post_id, field, edited_at) VALUES (1, 'url', 'now')"
        )

    assert [tuple(row) for row in rows] == [
        ("https://www.instagram.com/p/Old/", None, None),
        (None, "https://www.reddit.com/comments/abc/", "reddit"),
        (None, None, None),
    ]
    assert [tuple(edit) for edit in edits] == [(1, "caption")]


def test_launch_gives_every_link_its_key(settings):
    settings.data_dir.mkdir(parents=True)
    with closing(db.connect(settings.db_path)) as conn:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO posts (url, saved_at) VALUES ('https://www.instagram.com/reel/A/', 'now')"
        )
        conn.commit()
    with TestClient(create_app(settings)):
        pass
    with closing(db.connect(settings.db_path)) as conn:
        row = conn.execute("SELECT normalized_url, platform FROM posts").fetchone()
    assert tuple(row) == ("https://www.instagram.com/p/A/", "instagram")
