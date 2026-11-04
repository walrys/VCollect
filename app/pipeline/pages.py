"""Readable copies of pages, as a browser's Reader View makes them: the main text and images
of a page without its navigation, ads or banners. Everything that reaches a readable copy has
been through sanitise(), or was built here from escaped text."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from urllib.parse import urljoin, urlsplit

import lxml.etree
import lxml.html
import nh3
import trafilatura

# Plain document markup only: no scripts, styles, frames, forms, media embeds or attributes
# beyond these.
TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "del", "details", "dl", "dt",
    "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins",
    "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "small", "strong", "sub", "summary", "sup",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}  # fmt: skip
ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
BLOCK_TAGS = {
    "article", "blockquote", "br", "dd", "div", "dt", "figcaption", "figure", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "p", "pre", "section", "tr",
}  # fmt: skip
LAZY_SOURCES = ("data-src", "data-lazy-src", "data-original", "data-url")
MIN_TEXT_CHARS = 200  # less than this to read: a page that shows nothing without JavaScript
MAX_IMAGES = 30


@dataclass
class Article:
    title: str
    author: str
    site: str  # the site's name, else its domain
    description: str
    image: str | None  # the page's share image (og:image), absolute
    html: str  # the main content, sanitised; '' when there is too little to read

    @property
    def kicker(self) -> str:
        """What the readable copy names above the title: the site and the author."""
        return " · ".join(dict.fromkeys(part for part in [self.site, self.author] if part))


def article(content: bytes, url: str) -> Article:
    """The page's title and description, and its main content as sanitised HTML."""
    tree = trafilatura.load_html(content)
    if tree is None:  # not HTML after all
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        return Article(title=host, author="", site=host, description="", image=None, html="")
    meta = trafilatura.extract_metadata(tree, default_url=url)
    body = trafilatura.extract(
        trafilatura.load_html(content),
        url=url,
        output_format="html",
        include_comments=False,
        include_images=True,
        include_links=True,
        include_formatting=True,
        include_tables=True,
    )
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    title = _clean(meta.title if meta else "") or host
    html = sanitise(body or "", url)
    if len(html_to_text(html)) < MIN_TEXT_CHARS:
        html = ""
    image = meta.image if meta else None
    return Article(
        title=title,
        author=_clean(meta.author if meta else ""),
        site=_clean(meta.sitename if meta else "") or host,
        description=_clean(meta.description if meta else ""),
        image=urljoin(url, image) if image else None,
        html=_drop_title_heading(html, title),
    )


def sanitise(fragment: str, base_url: str) -> str:
    """Keeps only TAGS and ATTRIBUTES, with every link and image made absolute and limited to
    http(s); links open in a new tab."""
    root = parse_fragment(fragment)
    if root is None:
        return ""
    for image in root.iter("img"):
        src = image.get("src") or ""
        if not src or src.startswith("data:"):
            lazy = next((image.get(name) for name in LAZY_SOURCES if image.get(name)), None)
            if lazy:
                image.set("src", lazy)
    root.make_links_absolute(base_url, resolve_base_href=False, handle_failures="discard")
    cleaned = nh3.clean(
        serialise(root),
        tags=TAGS,
        attributes=ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    # Images left without an address (data: URIs, say) show as broken boxes.
    root = parse_fragment(cleaned)
    if root is None:
        return ""
    for image in list(root.iter("img")):
        if not image.get("src"):
            _remove(image)
    return serialise(root)


def localise_images(html: str, save: Callable[[str], str | None]) -> tuple[str, list[str]]:
    """Points each image at its saved copy: `save(url)` returns the file it saved the image as,
    or None, and an image that could not be saved is dropped. At most MAX_IMAGES are kept."""
    root = parse_fragment(html)
    if root is None:
        return "", []
    saved: dict[str, str | None] = {}
    for image in list(root.iter("img")):
        url = image.get("src", "")
        if url not in saved:
            saved[url] = save(url) if len([f for f in saved.values() if f]) < MAX_IMAGES else None
        if saved[url]:
            image.set("src", saved[url])
        else:
            _remove(image)
    return serialise(root), [file for file in saved.values() if file]


def html_to_text(html: str) -> str:
    """The text of the HTML, one line per block, for search and the summary."""
    root = parse_fragment(html)
    if root is None:
        return ""
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag in BLOCK_TAGS:
            element.tail = "\n" + (element.tail or "")
    lines = (" ".join(line.split()) for line in root.text_content().splitlines())
    return "\n".join(line for line in lines if line)


# ----- Readable copies built from parts. Arguments are plain text unless named *_html. -----


def reader(
    *, title: str, kicker: str = "", body_html: str = "", lead_image: str | None = None,
    extra_html: str = "",
) -> str:  # fmt: skip
    """A readable copy: the page's site or author above its title, then its content."""
    head = f'<p class="reader-kicker">{escape(kicker)}</p>' if kicker else ""
    lead = (
        f'<figure class="reader-lead"><img src="{escape(lead_image)}" alt=""></figure>'
        if lead_image and "<img" not in body_html
        else ""
    )
    return (
        '<article class="reader">'
        f'<header class="reader-head">{head}<h1>{escape(title)}</h1></header>'
        f'{lead}<div class="reader-body">{body_html}</div>{extra_html}</article>'
    )


def profile_card(
    *, name: str, handle: str, avatar: str | None, facts: list[str | None], bio: str,
    links: list[str], note: str = "", extra_html: str = "",
) -> str:  # fmt: skip
    """An account's page: its picture, names, numbers (None when unknown), bio and links."""
    facts = [fact for fact in facts if fact]
    parts = ['<article class="reader reader-profile"><header class="reader-head">']
    if avatar:
        parts.append(f'<img class="reader-avatar" src="{escape(avatar)}" alt="">')
    parts.append(f"<h1>{escape(name)}</h1>")
    if handle:
        parts.append(f'<p class="reader-kicker">{escape(handle)}</p>')
    if facts:
        parts.append(f'<p class="reader-facts">{escape(" · ".join(facts))}</p>')
    parts.append("</header>")
    if note:
        parts.append(f'<p class="reader-note">{escape(note)}</p>')
    if bio.strip():
        parts.append(f'<div class="reader-body">{paragraphs(bio)}</div>')
    for link in links:
        if link.startswith(("http://", "https://")):
            parts.append(f'<p class="reader-link">{anchor(link, link)}</p>')
    parts.append(f"{extra_html}</article>")
    return "".join(parts)


def paragraphs(text: str) -> str:
    """Plain text as HTML paragraphs, keeping its line breaks."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    return "".join(f"<p>{escape(block).replace(chr(10), '<br>')}</p>" for block in blocks)


def anchor(url: str, text: str) -> str:
    return (
        f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer nofollow">'
        f"{escape(text)}</a>"
    )


def count(number: int | None, noun: str) -> str | None:
    """'8,740,000 subscribers'; None when the number is unknown."""
    if number is None:
        return None
    return f"{number:,} {noun if number == 1 else noun + 's'}"


def _drop_title_heading(html: str, title: str) -> str:
    """Pages often repeat their title as the first heading; the readable copy has its own."""
    root = parse_fragment(html)
    if root is None:
        return html
    first = next((child for child in root if isinstance(child.tag, str)), None)
    if first is not None and re.fullmatch(r"h[1-3]", first.tag):
        if _clean(first.text_content()).lower() == title.lower():
            _remove(first)
            return serialise(root)
    return html


def parse_fragment(fragment: str) -> lxml.html.HtmlElement | None:
    """The fragment inside a <div>, whether it came as a whole document or a part of one."""
    if not fragment or not fragment.strip():
        return None
    try:
        body = lxml.html.document_fromstring(fragment).find("body")
    except (lxml.etree.ParserError, ValueError):
        return None
    if body is None:
        return None
    body.tag = "div"
    return body


def serialise(root: lxml.html.HtmlElement) -> str:
    """The children of the wrapping <div>."""
    inner = (escape(root.text) if root.text else "") + "".join(
        lxml.html.tostring(child, encoding="unicode") for child in root
    )
    return inner.strip()


def _remove(element: lxml.html.HtmlElement) -> None:
    """Removes an element, keeping the text that followed it."""
    parent = element.getparent()
    if parent is None:
        return
    if element.tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())
