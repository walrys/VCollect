"""Link clean-up: one canonical form per post, which is also the duplicate key, and what kind
of page the link is."""

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.pipeline.errors import PostFailed

TRACKING_PARAMS = {"igsh", "igshid", "si", "feature", "fbclid", "gclid", "ref", "ref_src"}
INSTAGRAM_POST_KINDS = {"p", "reel", "reels", "tv"}
# First path segments of Instagram pages that are not someone's profile.
INSTAGRAM_PAGES = {
    "about", "accounts", "ar", "challenge", "developer", "direct", "emails", "explore", "legal",
    "p", "privacy", "reel", "reels", "session", "stories", "tv", "web",
}  # fmt: skip
INSTAGRAM_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# The site name of the links instaloader fetches. Instagram limits its API apart from the
# pages yt-dlp reads, so a wall there must never stop the reels, and one on reels never
# stops profiles.
INSTAGRAM_API = "Instagram's API"
REDDIT_HOSTS = {"reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "redd.it"}
HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}(:\d+)?$")


@dataclass(frozen=True)
class PostUrl:
    url: str
    platform: str  # instagram | youtube | reddit | other
    site: str  # human name for messages, e.g. "Instagram" or "reddit.com"
    kind: str = "post"  # post | profile (an account) | playlist | page (anything else)


def normalise(raw: str) -> PostUrl:
    raw = raw.strip()
    if not raw:
        raise PostFailed("This row has no link")
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    netloc = parts.netloc.lower()
    host = netloc.removeprefix("www.").removeprefix("m.")
    if not HOST.match(host):
        raise PostFailed(f"This row's link is not a link: {raw[:200]}")
    path = [segment for segment in parts.path.split("/") if segment]
    query = _clean_query(parts.query)

    if host in {"instagram.com", "instagr.am"}:
        return _instagram(path)
    if host in {"youtube.com", "youtu.be", "music.youtube.com"}:
        return _youtube(host, path, dict(query))
    if host in REDDIT_HOSTS or host.endswith(".reddit.com"):
        return _reddit(host, path)
    url = urlunsplit(("https", netloc, parts.path, urlencode(query), ""))
    return PostUrl(url, "other", host)


def index_links(conn: sqlite3.Connection) -> None:
    """Give every post its canonical link before processing, so duplicates resolve to the
    earliest saved row whichever order posts are processed in."""
    rows = conn.execute(
        "SELECT id, url FROM posts WHERE normalized_url IS NULL AND url != ''"
    ).fetchall()
    with conn:
        for post_id, url in rows:
            key, platform = canonical(url)
            if key is not None:
                conn.execute(
                    "UPDATE posts SET normalized_url = ?, platform = ? WHERE id = ?",
                    (key, platform, post_id),
                )


def canonical(raw: str) -> tuple[str | None, str | None]:
    """A link's canonical form and platform; (None, None) when it is not a link."""
    try:
        link = normalise(raw)
    except PostFailed:
        return None, None
    return link.url, link.platform


def link_changed(url: str, fetched_url: str | None) -> bool:
    """Whether a post's link no longer leads where its media was fetched from (`fetched_url`,
    canonical; None before anything was). A change only to tracking parameters is no change."""
    return fetched_url is not None and canonical(url)[0] != fetched_url


def _clean_query(query: str) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]


def _instagram(path: list[str]) -> PostUrl:
    # Also accepts the newer /<user>/reel/<code>/ form.
    for kind, code in zip(path, path[1:], strict=False):
        if kind in INSTAGRAM_POST_KINDS:
            return PostUrl(f"https://www.instagram.com/p/{code}/", "instagram", "Instagram")
    if path and path[0].lower() not in INSTAGRAM_PAGES and INSTAGRAM_USER.match(path[0]):
        # A profile, or one of its tabs (/reels/, /tagged/): usernames ignore case.
        url = f"https://www.instagram.com/{path[0].lower()}/"
        return PostUrl(url, "instagram", INSTAGRAM_API, "profile")
    url = "https://www.instagram.com/" + "".join(f"{segment}/" for segment in path)
    return PostUrl(url, "instagram", "Instagram", "page")


def _youtube(host: str, path: list[str], query: dict[str, str]) -> PostUrl:
    if host == "youtu.be":
        video_id = path[0] if path else ""
    elif path[:1] == ["watch"]:
        video_id = query.get("v", "")
    elif len(path) >= 2 and path[0] in {"shorts", "live", "embed", "v"}:
        video_id = path[1]
    else:
        video_id = ""
    if YOUTUBE_ID.match(video_id):
        return PostUrl(f"https://www.youtube.com/watch?v={video_id}", "youtube", "YouTube")

    if path[:1] == ["playlist"] and query.get("list"):
        url = f"https://www.youtube.com/playlist?list={query['list']}"
        return PostUrl(url, "youtube", "YouTube", "playlist")
    if path and path[0].startswith("@"):
        # A channel or one of its tabs (/videos, /about): handles ignore case.
        return PostUrl(
            f"https://www.youtube.com/{path[0].lower()}", "youtube", "YouTube", "profile"
        )
    if len(path) >= 2 and path[0] in {"channel", "c", "user"}:
        url = f"https://www.youtube.com/{path[0]}/{path[1]}"
        return PostUrl(url, "youtube", "YouTube", "profile")
    url = urlunsplit(("https", "www.youtube.com", "/" + "/".join(path), urlencode(query), ""))
    return PostUrl(url, "youtube", "YouTube", "page")


def _reddit(host: str, path: list[str]) -> PostUrl:
    """Every link to one Reddit post shares the form /comments/<id>/, whichever subreddit or
    comment it names. A share link (/r/<sub>/s/<code>) only says which post once followed,
    which needs a Reddit login, so it stays as it is until the download resolves it."""
    post_id = ""
    if host == "redd.it" and path:
        post_id = path[0]
    elif "comments" in path[:-1]:
        post_id = path[path.index("comments") + 1]
    elif path[:1] == ["gallery"] and len(path) >= 2:
        post_id = path[1]
    if post_id:
        return PostUrl(f"https://www.reddit.com/comments/{post_id}/", "reddit", "reddit.com")
    if len(path) >= 4 and path[0] in {"r", "u", "user"} and path[2] == "s":
        url = "https://www.reddit.com/" + "/".join(path[:4])
        return PostUrl(url, "reddit", "reddit.com")
    url = "https://www.reddit.com/" + "".join(f"{segment}/" for segment in path)
    return PostUrl(url, "reddit", "reddit.com", "page")


def is_reddit_share(link: PostUrl) -> bool:
    return link.platform == "reddit" and "/s/" in link.url
