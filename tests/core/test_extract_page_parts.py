from progdata_crawler.core.extract.clean_html import clean_html
from progdata_crawler.core.extract.images import extract_images
from progdata_crawler.core.extract.links import absolute_url, extract_links
from progdata_crawler.core.extract.meta import extract_metadata
from progdata_crawler.models import ContentMode, Image, Link, SelectorFilter
from tests.support.site_server import STATIC_DIR

BASE = "https://www.example.com/docs/page.html"


def test_links_on_the_test_site_index() -> None:
    links = extract_links((STATIC_DIR / "index.html").read_text(), "http://test.site/")
    assert [(link.url, link.internal) for link in links] == [
        ("http://test.site/", True),
        ("http://test.site/article.html", True),
        ("http://test.site/docs/", True),
        ("http://test.site/private/staff.html", True),
        ("https://external.invalid/partner", False),
    ]
    assert links[1].text == "Our sourdough"


def test_link_rules() -> None:
    html = """
    <a href="intro.html#part-2">Intro, <b>part</b>
        two</a>
    <a href="intro.html">Duplicate</a>
    <a href="#top">Top</a>
    <a href="javascript:void(0)">JS</a>
    <a href="mailto:hi@example.com">Mail</a>
    <a href="tel:+4712345678">Phone</a>
    <a href="//cdn.example.org/file">Protocol-relative</a>
    <a href="https://example.com/no-www">Without www</a>
    <a href="/icon" title="Home page"><img src="i.png"></a>
    <a href="https://partner.example/" rel="Nofollow  sponsored">Partner</a>
    <a>No href</a>
    """
    assert extract_links(html, BASE) == (
        Link(url="https://www.example.com/docs/intro.html", text="Intro, part two", internal=True),
        Link(url="https://cdn.example.org/file", text="Protocol-relative", internal=False),
        Link(url="https://example.com/no-www", text="Without www", internal=True),
        Link(url="https://www.example.com/icon", text="Home page", internal=True),
        Link(
            url="https://partner.example/",
            text="Partner",
            internal=False,
            rel=("nofollow", "sponsored"),
        ),
    )


def test_base_href_is_honoured() -> None:
    html = '<head><base href="https://static.example.com/v2/"></head><a href="a.html">A</a>'
    assert extract_links(html, BASE)[0].url == "https://static.example.com/v2/a.html"


def test_absolute_url() -> None:
    assert absolute_url(BASE, " ../x?y=1#z ") == "https://www.example.com/x?y=1"
    assert absolute_url(BASE, "ftp://example.com/") is None
    assert absolute_url(BASE, "data:image/png;base64,AAAA") is None
    assert absolute_url(BASE, "") is None


def test_images() -> None:
    html = """
    <img src="/a.png" alt=" Logo   of the  bakery " width="120" height="40px">
    <img src="/a.png" alt="duplicate">
    <img data-src="lazy.jpg">
    <img srcset="small.jpg 480w, large.jpg 1080w">
    <img src="data:image/gif;base64,R0lGOD">
    <img alt="no source">
    <img src="b.png" width="auto">
    """
    assert extract_images(html, BASE) == (
        Image(url="https://www.example.com/a.png", alt="Logo of the bakery", width=120, height=40),
        Image(url="https://www.example.com/docs/lazy.jpg"),
        Image(url="https://www.example.com/docs/small.jpg"),
        Image(url="https://www.example.com/docs/b.png"),
    )


def test_metadata_of_the_test_site_article() -> None:
    html = (STATIC_DIR / "article.html").read_text()
    meta = extract_metadata(
        html, "http://test.site/article.html", status=200, content_type="text/html"
    )
    assert meta.title == "How we bake our sourdough"
    assert meta.description == "Three days from starter to loaf."
    assert meta.language == "en"
    assert meta.canonical_url == "http://test.site/article.html"
    assert meta.open_graph == {
        "title": "How we bake our sourdough",
        "type": "article",
        "image": "http://test.site/images/loaf.jpg",
    }
    assert (meta.status, meta.content_type) == (200, "text/html")


def test_metadata_fallbacks_and_extras() -> None:
    html = """
    <html lang=" nb-NO "><head>
      <meta property="og:title" content="From Open Graph">
      <meta property="og:description" content="OG description">
      <meta name="AUTHOR" content="Kari Nordmann">
      <meta name="keywords" content="bread, sourdough, , baking">
      <meta property="article:published_time" content="2026-11-01T08:00:00Z">
      <meta name="description" content="   ">
      <link rel="alternate canonical" href="/canonical">
    </head><body></body></html>
    """
    meta = extract_metadata(html, BASE)
    assert meta.title == "From Open Graph"
    assert meta.description == "OG description"
    assert meta.language == "nb-NO"
    assert meta.author == "Kari Nordmann"
    assert meta.keywords == ("bread", "sourdough", "baking")
    assert meta.published_time == "2026-11-01T08:00:00Z"
    assert meta.canonical_url == "https://www.example.com/canonical"


def test_metadata_of_an_empty_page() -> None:
    meta = extract_metadata("", BASE)
    assert meta.title is None
    assert meta.open_graph == {}


DIRTY = """
<html><head><style>p { color: red }</style><script>track()</script></head>
<body>
  <nav><a href="/">Home</a></nav>
  <main>
    <h1 onclick="steal()" style="color: red" class="title">Title</h1>
    <!-- a comment -->
    <p>Text with <a href="../faq.html" onmouseover="x()">a link</a>
       and <a href="javascript:alert(1)">a fake link</a>.</p>
    <img src="pic.png" srcset="pic-2x.png 2x" alt="Picture">
    <iframe src="https://ads.example/"></iframe>
    <noscript>Enable JS</noscript>
  </main>
  <footer>Footer</footer>
</body></html>
"""


def test_clean_html_main_content() -> None:
    cleaned = clean_html(DIRTY, BASE)
    assert cleaned.startswith("<main>")
    for gone in (
        "script",
        "style=",
        "onclick",
        "onmouseover",
        "iframe",
        "noscript",
        "comment",
        "srcset",
        "javascript:",
        "Footer",
        "Home",
    ):
        assert gone not in cleaned, gone
    assert 'class="title"' in cleaned
    assert 'href="https://www.example.com/faq.html"' in cleaned
    assert 'src="https://www.example.com/docs/pic.png"' in cleaned


def test_clean_html_full_and_include() -> None:
    full = clean_html(DIRTY, BASE, content=ContentMode.FULL)
    assert "<nav>" in full
    assert "Footer" in full
    assert "<script" not in full
    assert not full.startswith("<body")

    only_heading = clean_html(DIRTY, BASE, tags=SelectorFilter(include=("h1",)))
    assert only_heading == '<h1 class="title">Title</h1>'


def test_clean_html_without_main_region_strips_boilerplate() -> None:
    html = "<body><nav>Menu</nav><div><p>Body text</p></div><footer>F</footer></body>"
    cleaned = clean_html(html, BASE)
    assert "Body text" in cleaned
    assert "Menu" not in cleaned
    assert "F</footer>" not in cleaned
