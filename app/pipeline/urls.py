"""Link clean-up: one canonical form per post, which is also the duplicate key."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.pipeline.errors import PostFailed

TRACKING_PARAMS = {"igsh", "igshid", "si", "feature", "fbclid", "gclid", "ref", "ref_src"}
INSTAGRAM_POST_KINDS = {"p", "reel", "reels", "tv"}
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class PostUrl:
    url: str
    platform: str  # instagram | youtube | other
    site: str  # human name for messages, e.g. "Instagram" or "reddit.com"


def normalise(raw: str) -> PostUrl:
    raw = raw.strip()
    if not raw:
        raise PostFailed("This row has no link")
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path = [segment for segment in parts.path.split("/") if segment]

    if host in {"instagram.com", "instagr.am"}:
        return _instagram(path)
    if host in {"youtube.com", "youtu.be", "music.youtube.com"}:
        return _youtube(host, path, dict(parse_qsl(parts.query)))

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    url = urlunsplit(("https", parts.netloc.lower(), parts.path, urlencode(query), ""))
    return PostUrl(url, "other", host)


def _instagram(path: list[str]) -> PostUrl:
    # Also accepts the newer /<user>/reel/<code>/ form.
    for kind, code in zip(path, path[1:], strict=False):
        if kind in INSTAGRAM_POST_KINDS:
            return PostUrl(f"https://www.instagram.com/p/{code}/", "instagram", "Instagram")
    raise PostFailed("Not a single post: an Instagram profile or page")


def _youtube(host: str, path: list[str], query: dict[str, str]) -> PostUrl:
    if host == "youtu.be":
        video_id = path[0] if path else ""
    elif path[:1] == ["watch"]:
        video_id = query.get("v", "")
    elif len(path) >= 2 and path[0] in {"shorts", "live", "embed", "v"}:
        video_id = path[1]
    else:
        video_id = ""
    if not YOUTUBE_ID.match(video_id):
        raise PostFailed("Not a single post: a YouTube playlist, channel or feed")
    return PostUrl(f"https://www.youtube.com/watch?v={video_id}", "youtube", "YouTube")
