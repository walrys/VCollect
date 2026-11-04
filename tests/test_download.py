import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import instaloader
import pytest
import yt_dlp

from app.pipeline import download as download_module
from app.pipeline.content import PageCopy
from app.pipeline.download import (
    INSTAGRAM_IMAGES,
    Download,
    Downloader,
    NotASingleVideo,
    RawItem,
    classify,
)
from app.pipeline.errors import LoginNeeded, PostFailed, PostNeedsLogin, TryLater
from app.pipeline.urls import normalise
from tests.fakes.web import UNREACHABLE, FakeWeb, answer_with, html, image


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


def page(*kinds):
    """What yt-dlp reads of an Instagram carousel of `kinds` (video, image or broken video)."""
    entries = [
        {
            "id": f"item{n}",
            "formats": [{"url": f"https://cdn/{n}.mp4"}] if kind == "video" else [],
            "duration": None if kind == "image" else 12.0,
            "thumbnails": [{"url": f"https://cdn/{n}-small.jpg"}, {"url": f"https://cdn/{n}.jpg"}],
        }
        for n, kind in enumerate(kinds, start=1)
    ]
    return {
        "_type": "playlist",
        "title": "Post by someone",
        "uploader": "Some One",
        "description": "A caption",
        "entries": entries,
    }


def fake_cdn(monkeypatch, refuse: str = "") -> list[str]:
    """Serves a JPEG for every image URL, except 403 for URLs containing `refuse`."""
    fetched = []

    def get(self, url):
        fetched.append(url)
        request = httpx.Request("GET", url)
        if refuse and refuse in url:
            return httpx.Response(403, request=request)
        headers = {"content-type": "image/jpeg"}
        return httpx.Response(200, content=b"jpeg", headers=headers, request=request)

    monkeypatch.setattr(httpx.Client, "get", get)
    return fetched


def download_page(monkeypatch, tmp_path, info, skip_sites=None):
    """Downloads an Instagram post yt-dlp read as `info`; instaloader answers "instaloader"."""
    used = []

    def fake_yt_dlp(self, post, dest):
        raise NotASingleVideo(info)

    def fake_instaloader(self, post, dest):
        used.append("instaloader")
        return "instaloader"

    monkeypatch.setattr(Downloader, "_yt_dlp", fake_yt_dlp)
    monkeypatch.setattr(Downloader, "_instaloader", fake_instaloader)
    downloader = Downloader(tmp_path / "cookies.txt")
    post = normalise("https://www.instagram.com/p/AbC/")
    result = asyncio.run(downloader.download(post, tmp_path / "raw", skip_sites or {}))
    return used, result


def test_image_posts_come_from_the_page_yt_dlp_read(monkeypatch, tmp_path):
    fetched = fake_cdn(monkeypatch)
    used, download = download_page(monkeypatch, tmp_path, page(*["image"] * 11))

    assert used == []
    assert fetched[:2] == ["https://cdn/1.jpg", "https://cdn/2.jpg"]  # the full size, last listed
    assert [item.file for item in download.items][-2:] == ["10.jpg", "11.jpg"]
    assert {item.kind for item in download.items} == {"image"}
    assert (download.title, download.uploader, download.caption) == (
        "Post by someone",
        "Some One",
        "A caption",
    )


def test_a_single_image_post_comes_from_the_page_too(monkeypatch, tmp_path):
    fake_cdn(monkeypatch)
    [image] = page("image")["entries"]
    used, download = download_page(monkeypatch, tmp_path, {**image, "title": "Post by someone"})
    assert (used, download.items) == ([], [RawItem("01.jpg", "image")])


@pytest.mark.parametrize("kinds", [("image", "video"), ("image", "broken video")])
def test_carousels_with_videos_go_to_instaloader(monkeypatch, tmp_path, kinds):
    fetched = fake_cdn(monkeypatch)
    used, result = download_page(monkeypatch, tmp_path, page(*kinds))
    assert (used, result, fetched) == (["instaloader"], "instaloader", [])


def test_a_run_that_instaloader_refused_stops_asking_it(monkeypatch, tmp_path):
    fake_cdn(monkeypatch)
    wall = LoginNeeded(INSTAGRAM_IMAGES, "Instagram is limiting anonymous requests", False)
    skip = {INSTAGRAM_IMAGES: wall}
    with pytest.raises(LoginNeeded, match="limiting anonymous requests") as caught:
        download_page(monkeypatch, tmp_path, page("image", "video"), skip)
    assert (caught.value.site, caught.value.needs_login) == (INSTAGRAM_IMAGES, False)

    # Posts yt-dlp can see still download.
    used, download = download_page(monkeypatch, tmp_path / "next", page("image", "image"), skip)
    assert (used, len(download.items)) == ([], 2)


def test_an_image_the_cdn_refuses_is_retried(monkeypatch, tmp_path):
    fake_cdn(monkeypatch, refuse="/2.jpg")
    with pytest.raises(TryLater, match="403"):
        download_page(monkeypatch, tmp_path, page("image", "image"))


@pytest.mark.parametrize(
    "info",
    [
        {**page("image")["entries"][0], "title": "an image post"},
        {**page("image", "video"), "entries": iter(page("image", "video")["entries"])},
    ],
)
def test_yt_dlp_hands_over_what_it_read_of_image_posts(monkeypatch, tmp_path, info):
    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", lambda self, url, **kwargs: info)
    with pytest.raises(NotASingleVideo) as caught:
        Downloader(tmp_path / "cookies.txt")._yt_dlp_fetch(
            normalise("https://www.instagram.com/p/AbC/"), {"quiet": True}
        )
    read = caught.value.info
    assert read.get("title") == info.get("title")
    if "entries" in info:
        assert [entry["id"] for entry in read["entries"]] == ["item1", "item2"]


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
        # Only a login wall is one a session would solve; a rate limit passes.
        assert caught.value.needs_login == ("cookies.txt" in message)


def test_instaloader_gives_up_on_the_first_refusal(monkeypatch, tmp_path):
    # Left to itself it sleeps out a 429 for up to half an hour, holding up the Sync.
    built = []
    real = instaloader.Instaloader.__init__

    def spy(self, *args, **kwargs):
        built.append(kwargs.get("max_connection_attempts"))
        real(self, *args, **kwargs)

    monkeypatch.setattr(instaloader.Instaloader, "__init__", spy)
    fake_instagram(monkeypatch, instagram_post("GraphImage", ["https://cdn/photo.jpg"]))
    download_image_post(tmp_path)
    assert built == [1]


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


@pytest.mark.parametrize(
    "output, site",
    [
        ("ERROR: [Instagram] DdVargvpEQ8: This content isn't available to everyone: It can't be"
         " seen by certain audiences.", "Instagram"),
        ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for"
         " some users.", "YouTube"),
    ],
)  # fmt: skip
def test_an_age_wall_needs_a_login_for_that_post_alone(output, site):
    error = classify(output, site)
    # Not LoginNeeded: that would skip every other post of the site for the run.
    assert type(error) is PostNeedsLogin
    assert error.needs_login
    assert str(error).startswith(f"Age-restricted: {site} shows it only to a logged-in account")


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
    with_page = Download(
        items=[],
        title="t",
        uploader="u",
        caption="c",
        subtitles=None,
        thumbnail=None,
        page=PageCopy(html="<p>x</p>", text="x", images=["page/01.png"]),
        url="https://www.reddit.com/comments/abc/",
    )
    assert Download.from_json(with_page.to_json()) == with_page


# ----- Links that are not a video post -----

ARTICLE = (
    "<html><head><title>Notes</title><meta property='og:description' content='About bread'>"
    "</head><body><article><h1>Notes</h1>"
    + "".join(
        f"<p>Bread wants time, warmth and a lively starter; patience does the rest ({n}).</p>"
        for n in range(6)
    )
    + "<img src='/loaf.png'></article></body></html>"
)


def fetch(tmp_path, url, web, skip_sites=None):
    downloader = Downloader(tmp_path / "cookies.txt", web=web)
    dest = tmp_path / "raw"
    return asyncio.run(downloader.download(normalise(url), dest, skip_sites or {})), dest


def no_yt_dlp(monkeypatch):
    def refuse(self, post, dest):
        raise AssertionError(f"yt-dlp asked for {post.url}")

    monkeypatch.setattr(Downloader, "_yt_dlp", refuse)


def test_a_page_on_a_site_yt_dlp_does_not_know_skips_yt_dlp(monkeypatch, tmp_path, samples):
    no_yt_dlp(monkeypatch)
    png = samples["photo.png"].read_bytes()
    web = FakeWeb(
        {
            "https://bread.example/notes": html(ARTICLE, "https://bread.example/notes"),
            "https://bread.example/loaf.png": image(png),
        }
    )

    result, dest = fetch(tmp_path, "https://bread.example/notes?utm_source=x", web)

    assert (result.items, result.title, result.caption) == ([], "Notes", "About bread")
    assert "Bread wants time" in result.page.html and "Bread wants time" in result.page.text
    assert result.page.images == ["page/01.png"]
    assert '<img src="page/01.png"' in result.page.html
    assert (dest / "page" / "01.png").read_bytes() == png


def test_a_page_with_nothing_to_read_keeps_its_description(monkeypatch, tmp_path):
    no_yt_dlp(monkeypatch)
    shell = "<html><head><title>An app</title><meta name='description' content='Does things'>"
    web = FakeWeb({"https://app.example/": html(shell + "</head><body></body></html>", "")})

    result, _ = fetch(tmp_path, "https://app.example/", web)

    assert (result.title, result.page) == ("An app", PageCopy("", "Does things", []))


def test_when_yt_dlp_finds_no_video_the_page_is_kept(monkeypatch, tmp_path):
    def text_tweet(self, post, dest):
        raise download_module.NoMedia("No video could be found in this tweet")

    monkeypatch.setattr(Downloader, "_yt_dlp", text_tweet)
    url = "https://x.com/someone/status/123"
    web = FakeWeb({url: html(ARTICLE, url)})

    result, _ = fetch(tmp_path, url, web)

    assert result.title == "Notes"
    assert web.calls[0] == url


def test_a_link_to_an_image_is_an_image_post(monkeypatch, tmp_path, samples):
    no_yt_dlp(monkeypatch)
    png = samples["photo.png"].read_bytes()
    web = FakeWeb({"https://cdn.example/art/sunset.png": image(png)})

    result, _ = fetch(tmp_path, "https://cdn.example/art/sunset.png", web)

    assert result.items == [RawItem("01.png", "image")]
    assert (result.title, result.page) == ("sunset.png", None)


@pytest.mark.parametrize(
    "answer, error, message",
    [
        (answer_with(404), PostFailed, r"gone \(404\)"),
        (answer_with(410), PostFailed, r"gone \(410\)"),
        (answer_with(403), PostFailed, r"example.com refused the download \(403\)"),
        (answer_with(503, b"<title>Just a moment...</title>"), PostFailed, "a bot check"),
        (answer_with(503), TryLater, "answered 503"),
        (answer_with(429), TryLater, "answered 429"),
        (UNREACHABLE, TryLater, "Could not reach"),
    ],
)
def test_what_a_site_says_decides_failed_or_later(monkeypatch, tmp_path, answer, error, message):
    no_yt_dlp(monkeypatch)
    web = FakeWeb({"https://example.com/page": answer})
    with pytest.raises(error, match=message) as caught:
        fetch(tmp_path, "https://example.com/page", web)
    assert type(caught.value) is error


def profile_answer(**user):
    return {
        "data": {
            "user": {
                "username": "indiegamejoe",
                "full_name": "Joe Indie",
                "biography": "I help indie games\nfind players",
                "category_name": "Marketing agency",
                "external_url": "https://joe.example",
                "bio_links": [{"url": "https://joe.example"}, {"url": "https://links.example/joe"}],
                "edge_followed_by": {"count": 72429},
                "edge_follow": {"count": 300},
                "edge_owner_to_timeline_media": {"count": 1234},
                "profile_pic_url_hd": "https://cdn.example/joe.jpg",
                "is_private": False,
                **user,
            }
        }
    }


def fake_profile(monkeypatch, answer):
    asked = []

    def get_json(self, path, params, **kwargs):
        asked.append((path, params))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(instaloader.InstaloaderContext, "get_json", get_json)
    return asked


def test_an_instagram_profile_is_one_request_and_a_card(monkeypatch, tmp_path, samples):
    asked = fake_profile(monkeypatch, profile_answer())
    picture = image(samples["photo.png"].read_bytes(), content_type="image/jpeg")
    web = FakeWeb({"https://cdn.example/joe.jpg": picture})

    result, _ = fetch(tmp_path, "https://www.instagram.com/IndieGameJoe/", web)

    assert asked == [("api/v1/users/web_profile_info/", {"username": "indiegamejoe"})]
    assert (result.title, result.uploader) == ("Joe Indie (@indiegamejoe)", "@indiegamejoe")
    assert result.caption == "I help indie games\nfind players"
    text = result.page.text
    assert "@indiegamejoe · Marketing agency" in text
    assert "72,429 followers · 1,234 posts · 300 following" in text
    assert text.count("https://joe.example") == 1 and "https://links.example/joe" in text
    assert result.page.images == ["page/01.jpg"]


def test_a_missing_profile_fails(monkeypatch, tmp_path):
    fake_profile(monkeypatch, instaloader.exceptions.QueryReturnedNotFoundException("404"))
    with pytest.raises(PostFailed, match="no profile @nobody"):
        fetch(tmp_path, "https://www.instagram.com/nobody/", FakeWeb())


def test_a_rate_limited_profile_waits_and_the_run_stops_asking(monkeypatch, tmp_path):
    refusal = instaloader.exceptions.ConnectionException("JSON Query: 429 Too Many Requests")
    fake_profile(monkeypatch, refusal)
    with pytest.raises(LoginNeeded) as caught:
        fetch(tmp_path, "https://www.instagram.com/someone/", FakeWeb())
    assert (caught.value.site, caught.value.needs_login) == (INSTAGRAM_IMAGES, False)

    asked = fake_profile(monkeypatch, profile_answer())
    skip = {INSTAGRAM_IMAGES: caught.value}
    with pytest.raises(LoginNeeded):
        fetch(tmp_path / "next", "https://www.instagram.com/other/", FakeWeb(), skip)
    assert asked == []


def test_a_youtube_channel_lists_its_latest_videos(monkeypatch, tmp_path):
    asked = []

    def extract_info(self, url, download=True, **kwargs):
        asked.append((url, self.params.get("extract_flat"), download))
        return {
            "channel": "3Blue1Brown",
            "uploader_id": "@3blue1brown",
            "channel_follower_count": 8740000,
            "description": "Math videos",
            "thumbnails": [{"id": "avatar_uncropped", "url": "https://yt.example/a.jpg"}],
            "entries": [
                {
                    "url": "https://www.youtube.com/watch?v=LKrwhjOI8Nc",
                    "title": "Math",
                    "duration": 2444,
                },
                {
                    "url": "https://www.youtube.com/watch?v=Nbwv5wHQoj0",
                    "title": "IMO",
                    "duration": 3116,
                },
            ],
        }

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)

    result, _ = fetch(tmp_path, "https://www.youtube.com/@3Blue1Brown", FakeWeb())

    assert asked == [("https://www.youtube.com/@3blue1brown/videos", "in_playlist", False)]
    assert (result.title, result.uploader, result.items) == ("3Blue1Brown", "@3blue1brown", [])
    assert "8,740,000 subscribers" in result.page.text
    assert "IMO 51:56" in result.page.text
    assert 'href="https://www.youtube.com/watch?v=LKrwhjOI8Nc"' in result.page.html
