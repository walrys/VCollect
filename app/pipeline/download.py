"""Downloaders. yt-dlp takes every video post and the images it can see; instaloader the
Instagram carousels that mix videos and images, and profiles. Everything else is a page,
saved as a readable copy: Reddit posts (from Reddit's JSON, with their video through yt-dlp),
YouTube channels and playlists, and any other link. Of YouTube videos only Shorts are
downloaded; the rest are kept as a preview until their video is asked for."""

import asyncio
import functools
import json
import shutil
from collections.abc import Callable, Mapping
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

import httpx
import instaloader
import yt_dlp

from app.pipeline import pages, reddit
from app.pipeline.content import Download, PageCopy, RawItem
from app.pipeline.errors import LoginNeeded, PostFailed, PostNeedsLogin, TryLater
from app.pipeline.transcribe import vtt_to_text
from app.pipeline.urls import INSTAGRAM_API, PostUrl, is_reddit_share, normalise
from app.pipeline.web import Response, Web, has_session

__all__ = ["Download", "Downloader", "PageCopy", "RawItem", "classify"]

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
LIST_ENTRIES = 30  # videos listed in a channel's or playlist's readable copy

# instaloader's walls are named apart from "Instagram": they must never stop the run fetching
# reels or the image posts yt-dlp can see. Profiles are only ever fetched through it.
INSTAGRAM_IMAGES = INSTAGRAM_API
INSTAGRAM_RATE_LIMIT = (
    "Instagram is limiting anonymous requests for profiles and carousels with videos: they"
    " wait for the next Sync"
)
REDDIT_REFUSED = "reddit.com refused the session in cookies.txt: export its cookies again"

# Lower-cased fragments of downloader errors. Age walls come first: YouTube's says "sign in".
AGE_HINTS = (
    "isn't available to everyone",
    "can't be seen by certain audiences",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
)
LOGIN_HINTS = ("login", "log in", "sign in", "authentication is required", "--cookies", "401")
GONE_HINTS = (
    "private",
    "unavailable",
    "has been removed",
    "not available",
    "does not exist",
    "not found",
    "could not be found",
    "notfounderror",
    "404",
    "unsupported url",
    "no video formats found",
    "no video in this post",
    "account has been terminated",
)
# What yt-dlp says when a link on a site it knows is not a video: the page is saved instead.
NO_MEDIA_HINTS = (
    "no video",
    "no media",
    "unsupported url",
    "no formats",
    "not a video",
    "is offline",
    "not currently live",
    "will begin",
)


class NotASingleVideo(Exception):
    """yt-dlp found an Instagram image post or carousel. `info` is what it read of the post."""

    def __init__(self, info: dict | None = None):
        super().__init__("Not a single video")
        self.info = info or {}


class NoMedia(Exception):
    """yt-dlp found nothing to download at a link that is not a video site's post."""


class Downloader:
    """`web` opens a browsing session for pages and images; tests pass a fake."""

    def __init__(
        self, cookies: Path, max_duration_min: int = 0, web: Callable[[], Web] | None = None
    ):
        self.cookies = cookies
        self.max_duration_min = max_duration_min
        self._web = web or (lambda: Web(cookies))

    async def download(
        self,
        post: PostUrl,
        dest: Path,
        skip_sites: Mapping[str, LoginNeeded] = MappingProxyType({}),
        video: bool = False,
    ) -> Download:
        """`skip_sites` maps each site this run stopped asking to the wall it met there.
        A YouTube video that is not a Short comes back as a preview, unless `video` asks for
        the video itself."""
        dest.mkdir(parents=True, exist_ok=True)
        if post.platform == "reddit":
            if not has_session(self.cookies, post.url):
                raise LoginNeeded(post.site)
            if post.kind == "post":
                return await asyncio.to_thread(self._reddit, post, dest)
            return await asyncio.to_thread(self._web_page, post, dest)
        if post.platform == "instagram" and post.kind == "profile":
            if INSTAGRAM_IMAGES in skip_sites:
                raise skip_sites[INSTAGRAM_IMAGES].again()
            return await asyncio.to_thread(self._instagram_profile, post, dest)
        if post.platform == "youtube" and post.kind in {"profile", "playlist"}:
            return await asyncio.to_thread(self._youtube_list, post, dest)
        if post.kind == "page" or (post.platform == "other" and not has_site_extractor(post.url)):
            return await asyncio.to_thread(self._web_page, post, dest)
        preview = post.platform == "youtube" and not video
        try:
            return await asyncio.to_thread(self._yt_dlp, post, dest, preview)
        except NotASingleVideo as found:
            _empty(dest)
            if urls := _image_urls(found.info):
                return await asyncio.to_thread(self._save_images, found.info, urls, dest)
            if INSTAGRAM_IMAGES in skip_sites:
                raise skip_sites[INSTAGRAM_IMAGES].again() from None
            return await asyncio.to_thread(self._instaloader, post, dest)
        except NoMedia:
            _empty(dest)
            return await asyncio.to_thread(self._web_page, post, dest)

    # ----- yt-dlp -----

    def _yt_dlp(self, post: PostUrl, dest: Path, preview: bool = False) -> Download:
        """`preview` keeps a YouTube video that is not a Short as a preview: its thumbnail,
        captions and details, without the video."""
        options = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "format": "bv*+ba/b",
            # Largest stream whose shorter side is at most 720 px: compression keeps it there.
            "format_sort": ["res:720"],
            "merge_output_format": "mp4",
            "outtmpl": {"default": str(dest / "01.%(ext)s")},
            "writethumbnail": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "vtt",
        }
        if self.cookies.exists():
            options["cookiefile"] = str(self.cookies)
        if post.platform == "instagram":
            # Instagram blocks anonymous clients that ask too fast; a backlog is hundreds of reels.
            # Its single-file MP4 is H.264 + AAC at <=720p, which compression copies as it is;
            # the separate streams are VP9 (a re-encode) and wait twice, once per stream.
            options |= {"format": "b/bv*+ba", "sleep_interval": 3, "max_sleep_interval": 8}
        try:
            info = self._yt_dlp_fetch(post, options, preview)
        except yt_dlp.utils.DownloadError as error:
            text = str(error).lower()
            if post.platform == "instagram" and "no video formats found" in text:
                raise NotASingleVideo from error
            if _pages_too(post) and any(hint in text for hint in NO_MEDIA_HINTS):
                raise NoMedia(str(error)) from error
            if "subtitles" not in text:
                raise classify(str(error), post.site) from error
            # Subtitles are a bonus (YouTube rate-limits them on their own); whisper covers.
            options = {**options, "writesubtitles": False, "writeautomaticsub": False}
            try:
                info = self._yt_dlp_fetch(post, options, preview)
            except yt_dlp.utils.DownloadError as retry_error:
                raise classify(str(retry_error), post.site) from retry_error

        thumbnails = [path for path in sorted(dest.iterdir()) if path.suffix in IMAGE_EXTENSIONS]
        subtitle_files = sorted(dest.glob("*.vtt"))
        details = {
            "title": info.get("title") or "",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "caption": info.get("description") or "",
            "subtitles": vtt_to_text(subtitle_files[0].read_text()) if subtitle_files else None,
            "thumbnail": thumbnails[0].name if thumbnails else None,
        }
        if preview and _long_form(info):
            return Download(items=[], **details, preview=True, duration=info.get("duration"))
        items = [item for item in _media_in(dest) if item.kind == "video"]
        if not items:
            if _pages_too(post):
                raise NoMedia("No video in this post")
            raise PostFailed("No video in this post")
        return Download(items=items, **details)

    def _yt_dlp_fetch(self, post: PostUrl, options: dict, preview: bool = False) -> dict:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(post.url, download=False, process=False)
            if info.get("_type") in {"playlist", "multi_video"}:
                if post.platform == "instagram":  # a carousel
                    raise NotASingleVideo({**info, "entries": list(info.get("entries") or [])})
                if _pages_too(post):
                    raise NoMedia("A list of videos")  # a game's trailers, say: keep the page
                raise PostFailed("Not a single post: a playlist or channel")
            if post.platform == "instagram" and _is_image(info):
                raise NotASingleVideo(info)
            if preview and _long_form(info):
                # The thumbnail and captions only: yt-dlp writes them before the video.
                ydl.params["skip_download"] = True
                return ydl.process_ie_result(info, download=True)
            if info.get("is_live") and _pages_too(post):
                raise NoMedia("A live stream")  # it would record until the stream ends
            if info.get("is_live"):
                raise TryLater("Live now: the video can be downloaded once the stream ends")
            minutes = (info.get("duration") or 0) / 60
            if self.max_duration_min and minutes > self.max_duration_min:
                raise PostFailed(
                    f"Too long: {minutes:.0f} min (MAX_DURATION_MIN is {self.max_duration_min})"
                )
            return ydl.process_ie_result(info, download=True)

    def _youtube_list(self, post: PostUrl, dest: Path) -> Download:
        """A channel or playlist: what it is and its latest videos, as a readable copy."""
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "playlistend": LIST_ENTRIES,
        }
        if self.cookies.exists():
            options["cookiefile"] = str(self.cookies)
        # A channel's own address lists its tabs; its Videos tab lists the videos.
        urls = [f"{post.url}/videos", post.url] if post.kind == "profile" else [post.url]
        for url in urls:
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                break
            except yt_dlp.utils.DownloadError as error:
                if url == urls[-1]:
                    raise classify(str(error), post.site) from error

        name = info.get("channel") or info.get("uploader") or info.get("title") or post.url
        description = info.get("description") or ""
        entries = [entry for entry in info.get("entries") or [] if entry.get("url")]
        videos = "".join(
            f"<li>{pages.anchor(entry['url'], entry.get('title') or entry['url'])}"
            + (f" <small>{_duration(entry['duration'])}</small>" if entry.get("duration") else "")
            + "</li>"
            for entry in entries[:LIST_ENTRIES]
        )
        listing = f'<section class="reader-list"><h2>Videos</h2><ol>{videos}</ol></section>'
        thumbnails = info.get("thumbnails") or []
        if post.kind == "profile":
            title = name
            avatar = next((t["url"] for t in thumbnails if t.get("id") == "avatar_uncropped"), None)
            html = pages.profile_card(
                name=name,
                handle=info.get("uploader_id") or "",
                avatar=avatar,
                facts=[pages.count(info.get("channel_follower_count"), "subscriber")],
                bio=description,
                links=[],
                extra_html=listing if videos else "",
            )
        else:
            title = info.get("title") or name
            largest = max(thumbnails, key=lambda t: t.get("width") or 0, default=None)
            facts = " · ".join(
                filter(None, [name, pages.count(info.get("playlist_count"), "video")])
            )
            html = pages.reader(
                title=title,
                kicker=facts,
                body_html=pages.paragraphs(description),
                lead_image=largest and largest.get("url"),
                extra_html=listing if videos else "",
            )
        with self._web() as web:
            page = self._page_copy(web, html, dest, referer=post.url)
        return Download(
            items=[],
            title=title,
            uploader=info.get("uploader_id") or name,
            caption=description,
            subtitles=None,
            thumbnail=None,
            page=page,
        )

    # ----- Instagram, through instaloader -----

    def _save_images(self, info: dict, urls: list[str], dest: Path) -> Download:
        """Saves an image post from the page yt-dlp read, without asking Instagram again."""
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            for number, url in enumerate(urls, start=1):
                try:
                    response = client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    raise TryLater(f"Download failed: {error}"[:500]) from error
                kind = response.headers.get("content-type", "").split(";")[0].strip()
                (dest / f"{number:02}{IMAGE_TYPES.get(kind, '.jpg')}").write_bytes(response.content)
        return Download(
            items=_media_in(dest),
            title=info.get("title") or "",
            uploader=info.get("uploader") or info.get("channel") or "",
            caption=info.get("description") or "",
            subtitles=None,
            thumbnail=None,
        )

    def _instaloader(self, post: PostUrl, dest: Path) -> Download:
        shortcode = post.url.rstrip("/").rsplit("/", 1)[-1]
        with self._instagram() as loader:
            try:
                found = instaloader.Post.from_shortcode(loader.context, shortcode)
                if found.typename == "GraphSidecar":
                    urls = [
                        node.video_url if node.is_video else node.display_url
                        for node in found.get_sidecar_nodes()
                    ]
                else:
                    urls = [found.video_url if found.is_video else found.url]
                for number, url in enumerate(urls, start=1):
                    # download_pic adds the extension; 01, 02, ... keep the carousel's order.
                    loader.download_pic(str(dest / f"{number:02}"), url, found.date_utc)
            except instaloader.exceptions.InstaloaderException as error:
                raise _instaloader_error(error) from error

        items = _media_in(dest)
        if len(items) < len(urls):
            raise TryLater(f"Download incomplete: {len(items)} of {len(urls)} items")
        return Download(
            items=items,
            title="",
            uploader=found.owner_username or "",
            caption=found.caption or "",
            subtitles=None,
            thumbnail=None,
        )

    def _instagram_profile(self, post: PostUrl, dest: Path) -> Download:
        """One request for the profile: names, numbers, bio, links and picture."""
        username = post.url.rstrip("/").rsplit("/", 1)[-1]
        with self._instagram() as loader:
            try:
                answer = loader.context.get_json(
                    "api/v1/users/web_profile_info/", params={"username": username}
                )
            except instaloader.exceptions.QueryReturnedNotFoundException as error:
                raise PostFailed(
                    f"Could not download: Instagram has no profile @{username}"
                ) from error
            except instaloader.exceptions.InstaloaderException as error:
                raise _instaloader_error(error) from error
        user = (answer.get("data") or {}).get("user")
        if not user:
            raise PostFailed(f"Could not download: Instagram has no profile @{username}")

        name = " ".join((user.get("full_name") or "").split()) or username
        bio = user.get("biography") or ""
        following = (user.get("edge_follow") or {}).get("count")
        links = [user.get("external_url") or ""]
        links += [link.get("url") or "" for link in user.get("bio_links") or []]
        html = pages.profile_card(
            name=name,
            handle=" · ".join(filter(None, [f"@{username}", user.get("category_name")])),
            avatar=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
            facts=[
                pages.count((user.get("edge_followed_by") or {}).get("count"), "follower"),
                pages.count((user.get("edge_owner_to_timeline_media") or {}).get("count"), "post"),
                f"{following:,} following" if following is not None else None,
            ],
            bio=bio,
            links=list(dict.fromkeys(link for link in links if link)),
            note="Private account" if user.get("is_private") else "",
        )
        with self._web() as web:
            page = self._page_copy(web, html, dest, referer="https://www.instagram.com/")
        return Download(
            items=[],
            title=f"{name} (@{username})" if name != username else f"@{username}",
            uploader=f"@{username}",
            caption=bio,
            subtitles=None,
            thumbnail=None,
            page=page,
        )

    def _instagram(self) -> instaloader.Instaloader:
        """An instaloader that gives up on the first refusal: by default it sleeps out a 429,
        for up to half an hour, and the Sync with it."""
        loader = instaloader.Instaloader(quiet=True, max_connection_attempts=1)
        self._sign_in(loader)
        return loader

    def _sign_in(self, loader: instaloader.Instaloader) -> None:
        """Gives instaloader the Instagram session in cookies.txt, if there is one."""
        if not self.cookies.exists():
            return
        jar = MozillaCookieJar(self.cookies)
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {cookie.name: cookie.value for cookie in jar if "instagram.com" in cookie.domain}
        if {"sessionid", "csrftoken"} <= cookies.keys():
            # instaloader only shows the name; the cookies carry the session.
            loader.load_session(cookies.get("ds_user_id", "cookies.txt"), cookies)

    # ----- Reddit -----

    def _reddit(self, post: PostUrl, dest: Path) -> Download:
        """The post from Reddit's JSON; its video, or the video a link post points at, through
        yt-dlp. Images are the media of a post without text, else part of its readable copy."""
        with self._web() as web:
            link = self._resolve_share(web, post) if is_reddit_share(post) else post
            response = web.get(reddit.json_url(link.url))
            _check_reddit(response)
            try:
                found = reddit.parse(json.loads(response.content), link.url)
            except (ValueError, KeyError, IndexError, TypeError) as error:
                raise LoginNeeded(post.site, REDDIT_REFUSED) from error  # a login page

            items, thumbnail, subtitles = [], None, None
            if found.has_video or (found.link and has_site_extractor(found.link)):
                try:
                    video = self._yt_dlp(link, dest)
                    items, thumbnail, subtitles = video.items, video.thumbnail, video.subtitles
                except (PostFailed, NoMedia, NotASingleVideo):
                    _empty(dest)  # the video is gone or unsupported: the post's page remains
            if not items and found.images and not found.body_html:
                items = _fetch_items(web, found.images, dest, referer=link.url)
            html = reddit.page_html(found, images_in_body=not items)
            page = self._page_copy(web, html, dest, referer=link.url)
        return Download(
            items=items,
            title=found.title,
            uploader=f"u/{found.author}" if found.author else "",
            caption="",
            subtitles=subtitles,
            thumbnail=thumbnail,
            page=page,
            url=link.url,
        )

    def _resolve_share(self, web: Web, post: PostUrl) -> PostUrl:
        """Follows a share link to the post it names."""
        response = web.get(post.url)
        if response.status == 404:
            raise PostFailed("Could not download: Reddit says this share link does not exist")
        resolved = normalise(response.url)
        if resolved.platform != "reddit" or resolved.kind != "post" or is_reddit_share(resolved):
            raise LoginNeeded(post.site, REDDIT_REFUSED)
        return resolved

    # ----- Any other page -----

    def _web_page(self, post: PostUrl, dest: Path) -> Download:
        """The page as a readable copy, or a link to it when it shows nothing without
        JavaScript. A link straight to an image is an image post, to a video a video post."""
        host = (urlsplit(post.url).hostname or "").removeprefix("www.")
        with self._web() as web:
            response = web.get(post.url)
            _check_answer(response, post)
            kind = response.content_type
            if kind in IMAGE_TYPES:
                (dest / f"01{IMAGE_TYPES[kind]}").write_bytes(response.content)
                return Download(
                    items=_media_in(dest),
                    title=_file_title(response.url),
                    uploader=host,
                    caption="",
                    subtitles=None,
                    thumbnail=None,
                )
            if kind.startswith(("video/", "audio/")):
                try:
                    return self._yt_dlp(post, dest)  # yt-dlp takes plain files too
                except NoMedia as error:
                    raise PostFailed(f"Could not download: no video in this {kind} file") from error
            if kind not in {"", "text/html", "application/xhtml+xml"}:
                return Download(  # a PDF, an archive: nothing to read here
                    items=[],
                    title=_file_title(response.url),
                    uploader=host,
                    caption="",
                    subtitles=None,
                    thumbnail=None,
                    page=PageCopy("", "", []),
                )

            found = pages.article(response.content, response.url)
            page = PageCopy("", found.description, [])
            if found.html:
                html = pages.reader(
                    title=found.title,
                    kicker=found.kicker,
                    body_html=found.html,
                    lead_image=found.image,
                )
                page = self._page_copy(web, html, dest, referer=response.url)
        return Download(
            items=[],
            title=found.title,
            uploader=found.author or found.site,
            caption=found.description,
            subtitles=None,
            thumbnail=None,
            page=page,
        )

    def _page_copy(self, web: Web, html: str, dest: Path, referer: str) -> PageCopy:
        """Saves the copy's images into page/ in the download folder and points it at them."""
        folder = dest / "page"
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []

        def save(url: str) -> str | None:
            response = _get_image(web, url, referer)
            if response is None:
                return None
            name = f"page/{len(saved) + 1:02}{IMAGE_TYPES[response.content_type]}"
            (dest / name).write_bytes(response.content)
            saved.append(name)
            return name

        html, images = pages.localise_images(html, save)
        return PageCopy(html=html, text=pages.html_to_text(html), images=images)


def classify(output: str, site: str) -> TryLater | PostFailed:
    """Turn a downloader's error output into the matching pipeline error."""
    text = output.lower()
    if any(hint in text for hint in AGE_HINTS):
        return PostNeedsLogin(
            f"Age-restricted: {site} shows it only to a logged-in account. Add its cookies to"
            " cookies.txt in the data folder."
        )
    if any(hint in text for hint in LOGIN_HINTS):
        return LoginNeeded(site)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    last = next(
        (line for line in reversed(lines) if "error" in line.lower()), lines[-1] if lines else ""
    )
    if any(hint in text for hint in GONE_HINTS):
        return PostFailed(f"Could not download: {last}"[:500])
    return TryLater(f"Download failed: {last}"[:500] or "Download failed with no output")


@functools.cache
def _site_extractors() -> tuple:
    return tuple(ie for ie in yt_dlp.extractor.gen_extractor_classes() if ie.ie_key() != "Generic")


def has_site_extractor(url: str) -> bool:
    """yt-dlp knows the site (TikTok, Vimeo, X ...): it goes first, and the page is the
    fallback. A site only its generic extractor would try is a page from the start."""
    return any(ie.suitable(url) for ie in _site_extractors())


def _long_form(info: dict) -> bool:
    """A YouTube video Sync keeps only a preview of: anything YouTube does not mark as a Short
    (yt-dlp's media_type), streams included, live or over."""
    return info.get("media_type") != "short"


def _pages_too(post: PostUrl) -> bool:
    """Links that are kept as a page when yt-dlp finds no video there."""
    return post.platform in {"other", "reddit"}


def _check_answer(response: Response, post: PostUrl) -> None:
    host = (urlsplit(response.url or post.url).hostname or "").removeprefix("www.")
    if response.ok:
        return
    status = response.status
    if post.platform == "reddit" and status in {401, 403}:
        raise LoginNeeded(post.site, REDDIT_REFUSED)
    if status in {404, 410}:
        raise PostFailed(f"Could not download: {host} says the page is gone ({status})")
    challenge = response.headers.get("cf-mitigated") == "challenge" or (
        b"Just a moment..." in response.content[:20_000]
    )
    if challenge:
        raise PostFailed(f"{host} refused the download (a bot check)")
    if status in {401, 403}:
        raise PostFailed(f"{host} refused the download ({status})")
    if status in {408, 425, 429} or status >= 500:
        raise TryLater(f"{host} answered {status}: trying again next Sync")
    raise PostFailed(f"Could not download: {host} answered {status}")


def _check_reddit(response: Response) -> None:
    if response.ok:
        return
    if response.status == 404:
        raise PostFailed("Could not download: Reddit says this post does not exist")
    if response.status == 403:
        try:
            reason = json.loads(response.content).get("reason")
        except (ValueError, AttributeError):
            reason = None
        if reason in {"private", "quarantined", "banned"}:
            raise PostFailed(f"Could not download: the subreddit is {reason}")
        raise LoginNeeded("reddit.com", REDDIT_REFUSED)
    if response.status == 429:
        raise LoginNeeded(
            "reddit.com", "reddit.com is limiting requests: they wait for the next Sync", False
        )
    raise TryLater(f"reddit.com answered {response.status}: trying again next Sync")


def _get_image(web: Web, url: str, referer: str) -> Response | None:
    """The image at `url`, or None when it cannot be had or is not a picture ffmpeg reads."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        response = web.get(url, referer=referer)
    except TryLater:
        return None
    if not response.ok or response.truncated or response.content_type not in IMAGE_TYPES:
        return None
    return response


def _fetch_items(web: Web, urls: list[str], dest: Path, referer: str) -> list[RawItem]:
    """A post's images as its media; one that cannot be had leaves the post for later."""
    for number, url in enumerate(urls, start=1):
        response = _get_image(web, url, referer)
        if response is None:
            raise TryLater(f"Download failed: could not fetch image {number} of {len(urls)}")
        (dest / f"{number:02}{IMAGE_TYPES[response.content_type]}").write_bytes(response.content)
    return _media_in(dest)


def _file_title(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return name or (urlsplit(url).hostname or url).removeprefix("www.")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _empty(folder: Path) -> None:
    shutil.rmtree(folder)
    folder.mkdir(parents=True)


def _is_image(item: dict | None) -> bool:
    """An item yt-dlp found no video in, though it has the pictures (yt-dlp's thumbnails)."""
    return bool(
        item
        and item.get("thumbnails")
        and not (item.get("formats") or item.get("url") or item.get("duration"))
    )


def _image_urls(info: dict) -> list[str]:
    """Each item's full-size image, when the post is all images yt-dlp has pictures of."""
    items = info.get("entries") or ([info] if info else [])
    if not all(_is_image(item) for item in items):
        return []  # videos among them, which instaloader fetches
    # yt-dlp lists thumbnails worst to best; Instagram's last is the uncropped original.
    return [item["thumbnails"][-1]["url"] for item in items]


def _instaloader_error(error: instaloader.exceptions.InstaloaderException) -> TryLater | PostFailed:
    text = str(error).lower()
    # Anonymous over-use gets 429, or 401 "Please wait a few minutes before you try again".
    if (
        isinstance(error, instaloader.exceptions.TooManyRequestsException)
        or "429" in text
        or ("401" in text and "please wait" in text)
    ):
        return LoginNeeded(INSTAGRAM_IMAGES, INSTAGRAM_RATE_LIMIT, needs_login=False)
    return classify(f"{type(error).__name__}: {error}", INSTAGRAM_IMAGES)


def _media_in(folder: Path) -> list[RawItem]:
    items = []
    for path in sorted(folder.iterdir()):
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            items.append(RawItem(path.name, "video"))
        elif suffix in IMAGE_EXTENSIONS:
            items.append(RawItem(path.name, "image"))
    return items
