"""Readable copies: what survives sanitising, what extraction keeps, and the templates."""

import pytest

from app.pipeline import pages

ARTICLE = """<!doctype html>
<html><head>
  <title>Sourdough basics | Bread Blog</title>
  <meta property="og:title" content="Sourdough basics">
  <meta property="og:site_name" content="Bread Blog">
  <meta name="author" content="Ada Baker">
  <meta property="og:description" content="Keeping a starter alive.">
  <meta property="og:image" content="/share.jpg">
  <script>track()</script>
</head><body>
  <nav><a href="/">Home</a> <a href="/about">About</a></nav>
  <article>
    <h1>Sourdough basics</h1>
    {paragraphs}
    <img src="/images/starter.jpg" alt="A jar of starter">
    <p>Read <a href="/more">more about rye</a>.</p>
  </article>
  <footer>Cookie settings · Privacy</footer>
</body></html>"""
WORDS = "A starter is flour and water that ferments. Feed it every day with equal weights. " * 3


def page_with(paragraphs: int) -> bytes:
    body = "".join(f"<p>{WORDS} Paragraph {n}.</p>" for n in range(paragraphs))
    return ARTICLE.replace("{paragraphs}", body).encode()


def test_sanitise_keeps_document_markup_and_nothing_that_runs():
    dirty = (
        '<div class="x" onclick="steal()"><p style="color:red">Hi <b>there</b></p>'
        '<a href="javascript:alert(1)">bad</a><a href="/rel">rel</a>'
        "<script>steal()</script><style>p{}</style><iframe src=x></iframe>"
        '<img src="data:image/png;base64,AAAA"><img data-src="/lazy.png" src="">'
        "<form><input name=q></form></div>"
    )
    clean = pages.sanitise(dirty, "https://example.com/a/page")

    assert "steal" not in clean and "style" not in clean and "iframe" not in clean
    assert "javascript" not in clean and "data:" not in clean and "<input" not in clean
    assert "<p>Hi <b>there</b></p>" in clean
    assert (
        '<a href="https://example.com/rel" target="_blank" rel="noopener noreferrer nofollow">'
        in clean
    )
    assert '<img src="https://example.com/lazy.png">' in clean
    assert clean.count("<img") == 1


def test_an_article_keeps_its_main_content():
    found = pages.article(page_with(4), "https://bread.example/posts/sourdough")

    assert (found.title, found.site, found.author) == (
        "Sourdough basics",
        "Bread Blog",
        "Ada Baker",
    )
    assert found.kicker == "Bread Blog · Ada Baker"
    assert found.description == "Keeping a starter alive."
    assert found.image == "https://bread.example/share.jpg"
    assert "Paragraph 3." in found.html
    assert "Cookie settings" not in found.html and "track()" not in found.html
    assert "<h1>" not in found.html  # the title heading: the readable copy has its own
    assert "https://bread.example/more" in found.html


def test_a_page_with_almost_nothing_to_read_has_no_readable_copy():
    found = pages.article(b"<html><head><title>App</title></head><body><div id=root></div>"
                          b"<noscript>Turn on JavaScript</noscript></body></html>",
                          "https://app.example/")  # fmt: skip
    assert (found.title, found.html) == ("App", "")


def test_localise_images_points_at_the_saved_files_and_drops_the_rest():
    html = (
        '<p>a</p><img src="https://x/1.png"><img src="https://x/2.png"><img src="https://x/1.png">'
    )
    saved = {"https://x/1.png": "page/01.png"}

    out, files = pages.localise_images(html, saved.get)

    assert out == '<p>a</p><img src="page/01.png"><img src="page/01.png">'
    assert files == ["page/01.png"]


def test_localise_images_stops_at_the_limit(monkeypatch):
    monkeypatch.setattr(pages, "MAX_IMAGES", 2)
    html = "".join(f'<img src="https://x/{n}.png">' for n in range(4))
    out, files = pages.localise_images(html, lambda url: url.rsplit("/", 1)[1])
    assert files == ["0.png", "1.png"]
    assert out.count("<img") == 2


def test_text_has_a_line_per_block():
    html = "<h1>Title</h1><p>One <b>bold</b> line</p><ul><li>a</li><li>b</li></ul>x<br>y"
    assert pages.html_to_text(html) == "Title\nOne bold line\na\nb\nx\ny"


@pytest.mark.parametrize("text", ['<script>alert("x")</script>', "Tom & Jerry <3"])
def test_templates_escape_plain_text(text):
    html = pages.reader(title=text, kicker=text, body_html="<p>body</p>")
    card = pages.profile_card(
        name=text, handle=text, avatar=None, facts=[text, None], bio=text, links=["javascript:x"]
    )
    for out in (html, card):
        assert "<script>" not in out and "&lt;" in out
    assert "javascript" not in card


def test_a_lead_image_shows_only_when_the_body_has_none():
    with_lead = pages.reader(title="T", body_html="<p>x</p>", lead_image="https://x/l.png")
    without = pages.reader(title="T", body_html='<img src="a">', lead_image="https://x/l.png")
    assert "reader-lead" in with_lead and "reader-lead" not in without


def test_bio_line_breaks_survive():
    assert pages.paragraphs("one\ntwo\n\nthree") == "<p>one<br>two</p><p>three</p>"
