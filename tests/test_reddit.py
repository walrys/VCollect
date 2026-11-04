"""Reddit posts: what the JSON gives, and how the downloader turns it into a post."""

import asyncio

import pytest

from app.pipeline import reddit
from app.pipeline.download import REDDIT_REFUSED, Downloader
from app.pipeline.errors import LoginNeeded, PostFailed
from app.pipeline.urls import normalise
from tests.fakes.web import FakeWeb, answer_with, image, json_answer

POST = "https://www.reddit.com/comments/abc123/"


def comment(body, author="someone", score=3, replies=()):
    return {
        "kind": "t1",
        "data": {
            "author": author,
            "body": body,
            "body_html": f'<div class="md"><p>{body}</p></div>',
            "score": score,
            "replies": {"kind": "Listing", "data": {"children": list(replies)}} if replies else "",
        },
    }


def listing(comments=(), **post):
    data = {
        "title": "Steam revenue  for our first game",
        "author": "northend",
        "subreddit_name_prefixed": "r/gamedev",
        "score": 812,
        "num_comments": 140,
        "is_self": True,
        "url": "https://www.reddit.com/r/gamedev/comments/abc123/steam_revenue/",
        "selftext_html": '<div class="md"><p>We made <strong>$40k</strong>.</p></div>',
        **post,
    }
    return [
        {"data": {"children": [{"kind": "t3", "data": data}]}},
        {"data": {"children": list(comments)}},
    ]


def test_a_text_post_keeps_its_text_and_comments():
    found = reddit.parse(
        listing(
            [
                comment("Mods here", author="AutoModerator"),
                comment("Great write-up", replies=[comment("Agreed", replies=[comment("Same")])]),
                comment("[deleted]"),
                {"kind": "more", "data": {}},
            ]
        ),
        POST,
    )

    assert found.title == "Steam revenue for our first game"
    assert found.kicker == "r/gamedev · u/northend · 812 points · 140 comments"
    assert found.body_html == "<p>We made <strong>$40k</strong>.</p>"
    assert (found.images, found.has_video, found.link) == ([], False, None)
    assert found.comments == 3
    assert "Mods here" not in found.comments_html and "[deleted]" not in found.comments_html
    assert found.comments_html.count('class="reader-replies"') == 2


def test_comments_stop_three_levels_down_and_at_fifty(monkeypatch):
    deep = comment("1", replies=[comment("2", replies=[comment("3", replies=[comment("4")])])])
    assert reddit.parse(listing([deep]), POST).comments == 3

    many = [comment(f"c{n}") for n in range(60)]
    assert reddit.parse(listing(many), POST).comments == 50


def test_a_gallery_lists_its_images_in_order():
    found = reddit.parse(
        listing(
            is_gallery=True,
            selftext_html=None,
            gallery_data={"items": [{"media_id": "b"}, {"media_id": "a"}, {"media_id": "gone"}]},
            media_metadata={
                "a": {"status": "valid", "s": {"u": "https://preview.redd.it/a.jpg"}},
                "b": {"status": "valid", "s": {"u": "https://preview.redd.it/b.jpg"}},
                "gone": {"status": "failed"},
            },
        ),
        POST,
    )
    assert found.images == ["https://preview.redd.it/b.jpg", "https://preview.redd.it/a.jpg"]
    assert found.body_html == ""


@pytest.mark.parametrize(
    "fields, video, link",
    [
        ({"is_video": True}, True, None),
        ({"crosspost_parent_list": [{"secure_media": {"reddit_video": {"x": 1}}}]}, True, None),
        (
            {"is_self": False, "url": "https://www.youtube.com/watch?v=dvUGDMxl8OU",
             "post_hint": "rich:video"},
            True,
            "https://www.youtube.com/watch?v=dvUGDMxl8OU",
        ),
        ({"is_self": False, "url": "https://blog.example/post"}, False, "https://blog.example/post"),
        ({"is_self": False, "url": "https://i.redd.it/pic.png", "post_hint": "image"}, False, None),
    ],
)  # fmt: skip
def test_what_a_post_points_at(fields, video, link):
    found = reddit.parse(listing(**fields), POST)
    assert (found.has_video, found.link) == (video, link)


def test_images_in_a_posts_text_are_shown():
    body = '<div class="md"><p>Look: <a href="https://preview.redd.it/x.png?w=1">https://preview.redd.it/x.png?w=1</a></p></div>'
    found = reddit.parse(listing(selftext_html=body), POST)
    assert '<img src="https://preview.redd.it/x.png?w=1" alt="">' in found.body_html


# ----- The downloader's Reddit path -----


@pytest.fixture
def cookies(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n.reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\tsecret\n"
    )
    return path


def download(cookies, web, url):
    downloader = Downloader(cookies, web=web)
    return asyncio.run(downloader.download(normalise(url), cookies.parent / "raw"))


def test_without_a_reddit_session_nothing_is_asked(tmp_path):
    web = FakeWeb()
    with pytest.raises(LoginNeeded) as wall:
        download(tmp_path / "cookies.txt", web, "https://www.reddit.com/r/gamedev/s/Z92cYYKKPT")
    assert (wall.value.site, wall.value.needs_login, web.calls) == ("reddit.com", True, [])


def test_a_share_link_resolves_to_its_post(cookies):
    share = "https://www.reddit.com/r/gamedev/s/Z92cYYKKPT"
    landed = "https://www.reddit.com/r/gamedev/comments/abc123/steam_revenue/?share_id=x"
    web = FakeWeb(
        {
            share: answer_with(200, b"<html></html>", url=landed),
            reddit.json_url(POST): json_answer(listing([comment("Nice")])),
        }
    )

    result = download(cookies, web, share)

    assert result.url == POST
    assert (result.items, result.title, result.uploader) == (
        [],
        "Steam revenue for our first game",
        "u/northend",
    )
    assert "We made" in result.page.html and "Nice" in result.page.html
    lines = result.page.text.splitlines()
    assert lines[:3] == [
        "r/gamedev · u/northend · 812 points · 140 comments",
        "Steam revenue for our first game",
        "We made $40k.",
    ]


def test_an_image_post_without_text_keeps_its_images_as_media(cookies, samples):
    png = samples["photo.png"].read_bytes()
    web = FakeWeb(
        {
            reddit.json_url(POST): json_answer(
                listing(
                    selftext_html=None,
                    is_self=False,
                    url="https://i.redd.it/p.png",
                    post_hint="image",
                )
            ),
            "https://i.redd.it/p.png": image(png),
        }
    )

    result = download(cookies, web, POST)

    assert [(item.file, item.kind) for item in result.items] == [("01.png", "image")]
    assert "<img" not in result.page.html  # the picture is the post's media, not its text


def test_a_text_posts_images_are_part_of_its_copy(cookies, samples):
    png = samples["photo.png"].read_bytes()
    web = FakeWeb(
        {
            reddit.json_url(POST): json_answer(
                listing(
                    is_gallery=True,
                    gallery_data={"items": [{"media_id": "a"}]},
                    media_metadata={
                        "a": {"status": "valid", "s": {"u": "https://preview.redd.it/a.png"}}
                    },
                )
            ),
            "https://preview.redd.it/a.png": image(png),
        }
    )

    result = download(cookies, web, POST)

    assert result.items == []
    assert result.page.images == ["page/01.png"]
    assert '<img src="page/01.png" alt="">' in result.page.html
    assert (cookies.parent / "raw" / "page" / "01.png").read_bytes() == png


@pytest.mark.parametrize(
    "answer, error, message",
    [
        (answer_with(403, b"<html>blocked</html>"), LoginNeeded, REDDIT_REFUSED),
        (answer_with(200, b"<html>log in</html>"), LoginNeeded, REDDIT_REFUSED),
        (json_answer({"reason": "private"}, status=403), PostFailed, "the subreddit is private"),
        (answer_with(404), PostFailed, "does not exist"),
    ],
)
def test_reddits_refusals(cookies, answer, error, message):
    web = FakeWeb({reddit.json_url(POST): answer})
    with pytest.raises(error, match=message):
        download(cookies, web, POST)
