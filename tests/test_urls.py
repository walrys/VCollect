import pytest

from app.pipeline.errors import PostFailed
from app.pipeline.urls import normalise


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
    assert (link.platform, link.site) == ("instagram", "Instagram")


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
    assert link.platform == "youtube"


def test_other_links_lose_tracking_parameters():
    link = normalise("https://www.reddit.com/r/gamedev/s/Z92cYYKKPT?utm_source=share&keep=1#top")
    assert link.url == "https://www.reddit.com/r/gamedev/s/Z92cYYKKPT?keep=1"
    assert (link.platform, link.site) == ("other", "reddit.com")


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("", "no link"),
        ("https://www.instagram.com/indiegamejoe", "Instagram profile"),
        ("https://www.youtube.com/playlist?list=PL123", "YouTube playlist"),
        ("https://www.youtube.com/@channel", "YouTube playlist"),
    ],
)
def test_links_that_are_not_a_post_fail(raw, reason):
    with pytest.raises(PostFailed, match=reason):
        normalise(raw)
