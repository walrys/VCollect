import pytest

from app.pipeline.errors import PostFailed
from app.pipeline.urls import INSTAGRAM_API, PostUrl, normalise


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/reel/De1zlGniBQp/",
        "https://www.instagram.com/reel/De1zlGniBQp/?igsh=MWQ1ZGUxMzBkMA==",
        "https://instagram.com/p/De1zlGniBQp",
        "https://www.instagram.com/someone/reel/De1zlGniBQp/",
        "instagram.com/reels/De1zlGniBQp/",
    ],
)
def test_instagram_links_share_one_form(raw):
    link = normalise(raw)
    assert link.url == "https://www.instagram.com/p/De1zlGniBQp/"
    assert (link.platform, link.site, link.kind) == ("instagram", "Instagram", "post")


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/IndieGameJoe",
        "https://www.instagram.com/indiegamejoe/?igsh=abc",
        "https://www.instagram.com/indiegamejoe/reels/",
    ],
)
def test_instagram_profiles_are_profiles(raw):
    assert normalise(raw) == PostUrl(
        "https://www.instagram.com/indiegamejoe/", "instagram", INSTAGRAM_API, "profile"
    )


def test_other_instagram_pages_are_pages():
    link = normalise("https://www.instagram.com/explore/tags/pixelart/")
    assert (link.url, link.kind) == ("https://www.instagram.com/explore/tags/pixelart/", "page")


@pytest.mark.parametrize(
    "raw",
    [
        "https://youtu.be/dvUGDMxl8OU?si=abc",
        "https://www.youtube.com/watch?v=dvUGDMxl8OU&t=42",
        "https://m.youtube.com/shorts/dvUGDMxl8OU",
        "https://youtube.com/live/dvUGDMxl8OU",
    ],
)
def test_youtube_links_share_one_form(raw):
    link = normalise(raw)
    assert link.url == "https://www.youtube.com/watch?v=dvUGDMxl8OU"
    assert (link.platform, link.kind) == ("youtube", "post")


@pytest.mark.parametrize(
    "raw, url, kind",
    [
        (
            "https://www.youtube.com/@3Blue1Brown/videos",
            "https://www.youtube.com/@3blue1brown",
            "profile",
        ),
        (
            "https://www.youtube.com/channel/UCYO_jab",
            "https://www.youtube.com/channel/UCYO_jab",
            "profile",
        ),
        ("https://youtube.com/c/Someone/about", "https://www.youtube.com/c/Someone", "profile"),
        (
            "https://www.youtube.com/playlist?list=PL123&si=x",
            "https://www.youtube.com/playlist?list=PL123",
            "playlist",
        ),
        ("https://www.youtube.com/feed/history", "https://www.youtube.com/feed/history", "page"),
    ],
)
def test_youtube_channels_and_playlists(raw, url, kind):
    link = normalise(raw)
    assert (link.url, link.platform, link.kind) == (url, "youtube", kind)


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.reddit.com/r/gamedev/comments/1abcde/steam_revenue/",
        "https://old.reddit.com/r/gamedev/comments/1abcde/steam_revenue/?utm_source=share",
        "https://www.reddit.com/r/gamedev/comments/1abcde/steam_revenue/kx2f9/",  # a comment
        "https://redd.it/1abcde",
        "https://www.reddit.com/gallery/1abcde",
    ],
)
def test_reddit_posts_share_one_form(raw):
    assert normalise(raw) == PostUrl(
        "https://www.reddit.com/comments/1abcde/", "reddit", "reddit.com", "post"
    )


def test_reddit_share_links_wait_to_be_resolved():
    link = normalise("https://www.reddit.com/r/gamedev/s/Z92cYYKKPT?utm_source=share")
    assert link == PostUrl(
        "https://www.reddit.com/r/gamedev/s/Z92cYYKKPT", "reddit", "reddit.com", "post"
    )


def test_a_subreddit_is_a_page():
    link = normalise("https://www.reddit.com/r/gamedev/")
    assert (link.url, link.platform, link.kind) == (
        "https://www.reddit.com/r/gamedev/",
        "reddit",
        "page",
    )


def test_other_links_lose_tracking_parameters():
    link = normalise("https://Example.com/a/b?utm_source=share&keep=1&fbclid=x#top")
    assert link == PostUrl("https://example.com/a/b?keep=1", "other", "example.com", "post")


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("", "no link"),
        ("   ", "no link"),
        ("an idea for later", "not a link"),
    ],
)
def test_rows_without_a_usable_link_fail(raw, reason):
    with pytest.raises(PostFailed, match=reason):
        normalise(raw)
