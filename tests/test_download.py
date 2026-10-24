import pytest

from app.pipeline.download import Download, RawItem, classify
from app.pipeline.errors import LoginNeeded, PostFailed, TryLater


@pytest.mark.parametrize(
    "output",
    [
        "[instagram][error] HTTP redirect to login page (https://www.instagram.com/accounts/login/)",
        "ERROR: [Reddit] 1wzv6kr: Account authentication is required. Use --cookies",
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
    ],
)
def test_login_walls_ask_for_cookies(output):
    error = classify(output, "Instagram")
    assert isinstance(error, LoginNeeded)
    assert "cookies.txt" in str(error)


@pytest.mark.parametrize(
    "output",
    [
        "ERROR: [youtube] abc: Video unavailable",
        "ERROR: [youtube] abc: Private video. Sign up for access",
        "[instagram][error] NotFoundError: Requested post could not be found",
        "ERROR: Unsupported URL: https://example.com/page",
    ],
)
def test_gone_posts_fail_for_good(output):
    error = classify(output, "YouTube")
    # "Private video. Sign up" mentions no login, so it is gone rather than login-walled.
    assert isinstance(error, PostFailed)


def test_anything_else_is_retried():
    error = classify("ERROR: Unable to download webpage: <urlopen error timed out>", "YouTube")
    assert type(error) is TryLater
    assert "timed out" in str(error)


def test_a_download_survives_its_manifest():
    download = Download(
        items=[RawItem("01.mp4", "video"), RawItem("02.jpg", "image")],
        title="t",
        uploader="u",
        caption="c",
        subtitles=None,
        thumbnail="01.webp",
    )
    assert Download.from_json(download.to_json()) == download
