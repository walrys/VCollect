"""Reddit posts as Reddit's own JSON describes them: title, text, images, video and the top
comments. Reddit answers only logged-in sessions, so these are read with cookies.txt."""

import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from app.pipeline import pages

TOP_COMMENTS = 50
COMMENT_DEPTH = 3  # a comment, its replies and theirs
REDDIT_MEDIA_HOSTS = {"i.redd.it", "preview.redd.it", "v.redd.it"}
SKIPPED_AUTHORS = {"AutoModerator"}


def json_url(post_url: str) -> str:
    """The JSON for a /comments/<id>/ link, with its comments in Reddit's "best" order."""
    return (
        f"{post_url.rstrip('/')}/.json?raw_json=1&sort=confidence&limit=500&depth={COMMENT_DEPTH}"
    )


@dataclass
class RedditPost:
    title: str
    author: str
    subreddit: str  # r/gamedev
    score: int | None
    comment_count: int | None
    body_html: str  # sanitised; '' for a post without text
    images: list[str]  # full-size image addresses, in the post's order
    has_video: bool
    link: str | None  # where a link post points, outside Reddit
    comments_html: str
    comments: int = 0  # how many comments comments_html holds

    @property
    def kicker(self) -> str:
        parts = [self.subreddit, f"u/{self.author}" if self.author else ""]
        parts += [
            pages.count(self.score, "point") or "",
            pages.count(self.comment_count, "comment") or "",
        ]
        return " · ".join(part for part in parts if part)


def parse(listing: list, post_url: str) -> RedditPost:
    data = listing[0]["data"]["children"][0]["data"]
    body = pages.sanitise(data.get("selftext_html") or "", post_url)
    body = _inline_images(body)
    target = data.get("url_overridden_by_dest") or data.get("url") or ""
    host = (urlsplit(target).hostname or "").lower()
    outside = bool(target) and not data.get("is_self") and not _reddit_host(host)
    comments_html, comments = _comments(listing[1]["data"]["children"] if len(listing) > 1 else [])
    return RedditPost(
        title=" ".join((data.get("title") or "").split()),
        author=data.get("author") or "",
        subreddit=data.get("subreddit_name_prefixed") or "",
        score=data.get("score"),
        comment_count=data.get("num_comments"),
        body_html=body,
        images=_images(data),
        has_video=_has_video(data),
        link=target if outside else None,
        comments_html=comments_html,
        comments=comments,
    )


def page_html(post: RedditPost, images_in_body: bool) -> str:
    """The readable copy: the post's text (with its images when `images_in_body`, as they are
    not media items then), a link post's target, and the comments."""
    body = post.body_html
    if images_in_body:
        body += "".join(f'<figure><img src="{escape(url)}" alt=""></figure>' for url in post.images)
    if post.link:
        body += f'<p class="reader-link">{pages.anchor(post.link, post.link)}</p>'
    extra = ""
    if post.comments_html:
        extra = f'<section class="reader-comments"><h2>Comments</h2>{post.comments_html}</section>'
    return pages.reader(title=post.title, kicker=post.kicker, body_html=body, extra_html=extra)


def _has_video(data: dict) -> bool:
    if data.get("is_video") or data.get("post_hint") in {"hosted:video", "rich:video"}:
        return True
    for source in [data, *(data.get("crosspost_parent_list") or [])]:
        for key in ("secure_media", "media"):
            if (source.get(key) or {}).get("reddit_video"):
                return True
    return False


def _images(data: dict) -> list[str]:
    """A gallery's images in order, or the single image of an image post."""
    metadata = data.get("media_metadata") or {}
    if data.get("is_gallery"):
        urls = []
        for item in (data.get("gallery_data") or {}).get("items", []):
            media = metadata.get(item.get("media_id")) or {}
            source = media.get("s") or {}
            url = source.get("u") or source.get("gif")
            if media.get("status") == "valid" and url:
                urls.append(url)
        return urls
    target = data.get("url_overridden_by_dest") or data.get("url") or ""
    if data.get("post_hint") == "image" or re.search(r"\.(jpe?g|png|gif|webp)$", target, re.I):
        if (urlsplit(target).hostname or "") in REDDIT_MEDIA_HOSTS or data.get(
            "post_hint"
        ) == "image":
            return [target]
    return []


def _inline_images(body: str) -> str:
    """Images in a post's text come as links to them; show them instead."""
    root = pages.parse_fragment(body)
    if root is None:
        return body
    changed = False
    for link in list(root.iter("a")):
        href = link.get("href", "")
        if (
            urlsplit(href).hostname or ""
        ) in REDDIT_MEDIA_HOSTS and link.text_content().strip() in {
            href,
            "",
            "image",
        }:
            image = root.makeelement("img", {"src": href, "alt": ""})
            image.tail = link.tail
            link.getparent().replace(link, image)
            changed = True
    return pages.serialise(root) if changed else body


def _comments(children: list, depth: int = 0) -> tuple[str, int]:
    """Up to TOP_COMMENTS comments with their replies, COMMENT_DEPTH levels deep; returns the
    HTML and how many comments it holds."""
    parts, total, top = [], 0, 0
    for child in children:
        if child.get("kind") != "t1" or (depth == 0 and top >= TOP_COMMENTS):
            continue
        data = child.get("data") or {}
        if data.get("author") in SKIPPED_AUTHORS:
            continue
        replies_html, replies = "", 0
        if depth + 1 < COMMENT_DEPTH and isinstance(data.get("replies"), dict):
            replies_html, replies = _comments(data["replies"]["data"]["children"], depth + 1)
        body = pages.sanitise(data.get("body_html") or "", "https://www.reddit.com/")
        if data.get("body") in {"[deleted]", "[removed]"} and not replies:
            continue
        meta = " · ".join(
            part
            for part in [
                f"u/{data.get('author') or '[deleted]'}",
                pages.count(data.get("score"), "point"),
            ]
            if part
        )
        nested = f'<div class="reader-replies">{replies_html}</div>' if replies_html else ""
        parts.append(
            f'<div class="reader-comment"><p class="reader-comment-meta">{escape(meta)}</p>'
            f"{body}{nested}</div>"
        )
        total += 1 + replies
        top += 1
    return "".join(parts), total


def _reddit_host(host: str) -> bool:
    return host in REDDIT_MEDIA_HOSTS or host == "redd.it" or host.endswith("reddit.com")
