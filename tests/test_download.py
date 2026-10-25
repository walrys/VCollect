import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import instaloader
import pytest
import yt_dlp

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

    def fake_instaloader(self, post, dest):
        used.append("instaloader")
        assert list(dest.iterdir()) == []  # yt-dlp's leftovers are cleared first
        return "from instaloader"

    monkeypatch.setattr(Downloader, "_yt_dlp", fake_yt_dlp)
    monkeypatch.setattr(Downloader, "_instaloader", fake_instaloader)
    result = asyncio.run(
        Downloader(tmp_path / "cookies.txt").download(normalise(url), tmp_path / "raw")
    )
    return used, result


def test_instagram_reels_come_from_yt_dlp(monkeypatch, tmp_path):
    used, result = route(monkeypatch, tmp_path, "https://www.instagram.com/reel/AbC/", "a reel")
    assert (used, result) == (["yt-dlp"], "a reel")


def test_instagram_image_posts_fall_back_to_instaloader(monkeypatch, tmp_path):
    used, result = route(
        monkeypatch, tmp_path, "https://www.instagram.com/p/AbC/", NotASingleVideo()
    )
    assert (used, result) == (["yt-dlp", "instaloader"], "from instaloader")


@pytest.mark.parametrize(
    ("url", "wanted"),
    [
        # Instagram's single file is H.264 that compression copies; YouTube's is 360p.
        ("https://www.instagram.com/reel/AbC/", "b/bv*+ba"),
        ("https://youtu.be/dQw4w9WgXcQ", "bv*+ba/b"),
    ],
)
def test_yt_dlp_takes_instagrams_single_file(monkeypatch, tmp_path, url, wanted):
    formats = []

    def fake_fetch(self, post, options):
        formats.append(options["format"])
        raise yt_dlp.utils.DownloadError("ERROR: Video unavailable")

    monkeypatch.setattr(Downloader, "_yt_dlp_fetch", fake_fetch)
    with pytest.raises(PostFailed):
        Downloader(tmp_path / "cookies.txt")._yt_dlp(normalise(url), tmp_path)
    assert formats == [wanted]


def fake_instagram(monkeypatch, post, saved: set[int] | None = None):
    """Serves `post` to instaloader and saves what it downloads (only items in `saved`, if set)."""
    monkeypatch.setattr(instaloader.Post, "from_shortcode", lambda context, code: post)

    def fake_download_pic(self, filename, url, mtime):
        if saved is None or int(Path(filename).name) in saved:
            Path(f"{filename}.{url.rsplit('.', 1)[-1]}").write_bytes(b"media")
        return True

    monkeypatch.setattr(instaloader.Instaloader, "download_pic", fake_download_pic)


def instagram_post(typename, urls, **fields):
    nodes = [
        SimpleNamespace(is_video=url.endswith(".mp4"), video_url=url, display_url=url)
        for url in urls
    ]
    return SimpleNamespace(
        typename=typename,
        get_sidecar_nodes=lambda: iter(nodes),
        is_video=nodes[0].is_video,
        video_url=urls[0],
        url=urls[0],
        owner_username="someone",
        caption="A caption",
        date_utc=datetime(2026, 10, 1, tzinfo=UTC),
        **fields,
    )


def download_image_post(tmp_path):
    dest = tmp_path / "raw"
    dest.mkdir()
    post = normalise("https://www.instagram.com/p/AbC/")
    return Downloader(tmp_path / "cookies.txt")._instaloader(post, dest)


def test_instaloader_keeps_every_carousel_item_in_order(monkeypatch, tmp_path):
    urls = [f"https://cdn/{n}.jpg" for n in range(1, 12)]
    urls[2] = "https://cdn/3.mp4"
    fake_instagram(monkeypatch, instagram_post("GraphSidecar", urls))

    download = download_image_post(tmp_path)

    assert [(item.file, item.kind) for item in download.items][:4] == [
        ("01.jpg", "image"),
        ("02.jpg", "image"),
        ("03.mp4", "video"),
        ("04.jpg", "image"),
    ]
    assert download.items[-1].file == "11.jpg"  # after 10, not after 1
    assert (download.uploader, download.caption) == ("someone", "A caption")


def test_instaloader_saves_a_single_image(monkeypatch, tmp_path):
    fake_instagram(monkeypatch, instagram_post("GraphImage", ["https://cdn/photo.jpg"]))
    assert download_image_post(tmp_path).items == [RawItem("01.jpg", "image")]


def test_an_incomplete_carousel_is_retried(monkeypatch, tmp_path):
    urls = ["https://cdn/1.jpg", "https://cdn/2.jpg", "https://cdn/3.jpg"]
    fake_instagram(monkeypatch, instagram_post("GraphSidecar", urls), saved={1, 3})
    with pytest.raises(TryLater, match="2 of 3"):
        download_image_post(tmp_path)


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (
            instaloader.exceptions.ConnectionException(
                'JSON Query to graphql/query: 401 Unauthorized - "fail" status, message'
                ' "Please wait a few minutes before you try again."'
            ),
            LoginNeeded,
            "limiting anonymous requests",
        ),
        (instaloader.exceptions.TooManyRequestsException("429"), LoginNeeded, "limiting"),
        (
            instaloader.exceptions.LoginRequiredException("Redirected to login page."),
            LoginNeeded,
            "cookies.txt",
        ),
        (instaloader.exceptions.QueryReturnedNotFoundException("404 Not Found"), PostFailed, ""),
        (
            instaloader.exceptions.BadResponseException("Fetching Post metadata failed."),
            TryLater,
            "metadata failed",
        ),
    ],
)
def test_instaloader_errors(monkeypatch, tmp_path, error, expected, message):
    def refuse(context, code):
        raise error

    monkeypatch.setattr(instaloader.Post, "from_shortcode", refuse)
    with pytest.raises((TryLater, PostFailed)) as caught:
        download_image_post(tmp_path)
    assert type(caught.value) is expected
    assert message in str(caught.value)
    if expected is LoginNeeded:
        # Named apart from "Instagram", so the run keeps fetching reels.
        assert caught.value.site == INSTAGRAM_IMAGES != normalise("instagram.com/p/AbC/").site


def test_instaloader_signs_in_with_cookies_txt(monkeypatch, tmp_path):
    (tmp_path / "cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tsecret\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tcsrftoken\ttoken\n"
        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tds_user_id\t42\n"
        ".reddit.com\tTRUE\t/\tTRUE\t2000000000\treddit_session\tother\n"
    )
    sessions = []
    monkeypatch.setattr(
        instaloader.Instaloader,
        "load_session",
        lambda self, username, cookies: sessions.append((username, cookies)),
    )
    fake_instagram(monkeypatch, instagram_post("GraphImage", ["https://cdn/photo.jpg"]))

    download_image_post(tmp_path)

    assert sessions == [("42", {"sessionid": "secret", "csrftoken": "token", "ds_user_id": "42"})]


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
