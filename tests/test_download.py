import asyncio

import pytest

from app.pipeline.download import (
    INSTAGRAM_IMAGES,
    Download,
    Downloader,
    NotASingleVideo,
    RawItem,
    classify,
)
from app.pipeline.errors import LoginNeeded, PostFailed, TryLater
from app.pipeline.urls import normalise


def route(monkeypatch, tmp_path, url, yt_dlp_result):
    """Which downloader handles `url`, given what yt-dlp makes of it."""
    used = []

    def fake_yt_dlp(self, post, dest):
        used.append("yt-dlp")
        if isinstance(yt_dlp_result, Exception):
            raise yt_dlp_result
        return yt_dlp_result

    async def fake_gallery_dl(self, post, dest):
        used.append("gallery-dl")
        assert list(dest.iterdir()) == []  # yt-dlp's leftovers are cleared first
        return "from gallery-dl"

    monkeypatch.setattr(Downloader, "_yt_dlp", fake_yt_dlp)
    monkeypatch.setattr(Downloader, "_gallery_dl", fake_gallery_dl)
    result = asyncio.run(
        Downloader(tmp_path / "cookies.txt").download(normalise(url), tmp_path / "raw")
    )
    return used, result


def test_instagram_reels_come_from_yt_dlp(monkeypatch, tmp_path):
    used, result = route(monkeypatch, tmp_path, "https://www.instagram.com/reel/AbC/", "a reel")
    assert (used, result) == (["yt-dlp"], "a reel")


def test_instagram_image_posts_fall_back_to_gallery_dl(monkeypatch, tmp_path):
    used, result = route(
        monkeypatch, tmp_path, "https://www.instagram.com/p/AbC/", NotASingleVideo()
    )
    assert (used, result) == (["yt-dlp", "gallery-dl"], "from gallery-dl")


def test_gallery_dl_login_walls_are_named_apart_from_instagram():
    output = (
        "[instagram][error] HTTP redirect to login page (https://www.instagram.com/accounts/login/)"
    )
    error = classify(output, INSTAGRAM_IMAGES)
    assert isinstance(error, LoginNeeded)
    assert error.site == "Instagram image posts" != normalise("instagram.com/p/AbC/").site


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
