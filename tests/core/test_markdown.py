from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

from progdata_crawler.core.extract.html_markdown import html_to_markdown
from progdata_crawler.core.extract.markdown import to_markdown
from progdata_crawler.models import ContentMode, SelectorFilter
from tests.support.site_server import STATIC_DIR

GOLDEN_DIR = Path(__file__).parent / "golden"
BASE = "http://test.site/"


@pytest.mark.parametrize(
    ("golden", "page", "content", "tags"),
    [
        ("article-main.md", "article.html", ContentMode.MAIN, SelectorFilter()),
        (
            "article-full-without-boilerplate.md",
            "article.html",
            ContentMode.FULL,
            SelectorFilter(exclude=(".ads", "nav", "footer")),
        ),
        ("faq-main.md", "docs/faq.html", ContentMode.MAIN, SelectorFilter()),
        ("index-full.md", "index.html", ContentMode.FULL, SelectorFilter()),
    ],
)
def test_golden_files(golden: str, page: str, content: ContentMode, tags: SelectorFilter) -> None:
    html = (STATIC_DIR / page).read_text()
    expected = (GOLDEN_DIR / golden).read_text().rstrip("\n")
    assert to_markdown(html, BASE + page, content=content, tags=tags) == expected


def test_main_content_drops_boilerplate() -> None:
    markdown = to_markdown((STATIC_DIR / "article.html").read_text(), BASE + "article.html")
    assert "cinnamon buns" not in markdown  # ad
    assert "Opening hours" not in markdown  # footer
    assert "[Home]" not in markdown  # navigation
    assert "## Day two: the dough" in markdown


def test_include_keeps_only_selected_elements_in_document_order() -> None:
    html = (STATIC_DIR / "article.html").read_text()
    markdown = to_markdown(html, BASE, tags=SelectorFilter(include=("article ul", "h1")))
    assert markdown == (
        "# How we bake our sourdough\n\n"
        "- 1000 g wheat flour\n- 750 g water\n- 200 g starter\n- 22 g salt"
    )


def test_include_skips_elements_nested_in_another_selected_one() -> None:
    html = "<body><article><h1>Title</h1><p>Text</p></article></body>"
    markdown = to_markdown(html, BASE, tags=SelectorFilter(include=("h1", "article")))
    assert markdown == "# Title\n\nText"


def test_exclude_applies_to_main_content() -> None:
    html = (STATIC_DIR / "article.html").read_text()
    markdown = to_markdown(html, BASE, tags=SelectorFilter(exclude=("article ul",)))
    assert "1000 g wheat flour" not in markdown
    assert "## Day two" in markdown


def test_page_without_body_gives_empty_markdown() -> None:
    assert to_markdown("", BASE) == ""
    assert to_markdown("", BASE, content=ContentMode.FULL) == ""


def convert(html: str) -> str:
    tree = LexborHTMLParser(f"<html><body>{html}</body></html>")
    assert tree.body is not None
    return html_to_markdown(tree.body, "https://example.com/docs/page.html")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<h2>Title</h2><p>One  \n two</p>", "## Title\n\nOne two"),
        ("<p>Line one<br>line two</p>", "Line one\nline two"),
        (
            '<p>See <a href="../faq.html">the FAQ</a>.</p>',
            "See [the FAQ](https://example.com/faq.html).",
        ),
        ('<p><a href="javascript:void(0)">Click</a> <a href="#top">Top</a></p>', "Click Top"),
        (
            '<p><a href="/x"><img src="a.png" alt="A"></a></p>',
            "[![A](https://example.com/docs/a.png)](https://example.com/x)",
        ),
        (
            "<p>Some <strong>bold</strong> and <em>italic</em> and <code>x = 1</code>.</p>",
            "Some **bold** and *italic* and `x = 1`.",
        ),
        ("<pre><code>def f():\n    return 1\n</code></pre>", "```\ndef f():\n    return 1\n```"),
        ("<blockquote><p>Quoted</p><p>Again</p></blockquote>", "> Quoted\n>\n> Again"),
        ("<ol><li>First</li><li>Second</li></ol>", "1. First\n2. Second"),
        (
            "<ul><li>Fruit<ul><li>Apple</li><li>Pear</li></ul></li><li>Bread</li></ul>",
            "- Fruit\n\n  - Apple\n  - Pear\n- Bread",
        ),
        (
            "<table><tr><th>Item</th><th>Price</th></tr>"
            "<tr><td>Loaf</td><td>45 | 50</td></tr></table>",
            "| Item | Price |\n| --- | --- |\n| Loaf | 45 \\| 50 |",
        ),
        ("<hr>", "---"),
        ("<div>Loose <span>text</span></div><p>Para</p>", "Loose text\n\nPara"),
        (
            "<script>alert(1)</script><style>p{}</style><form><input value=x></form><p>Kept</p>",
            "Kept",
        ),
        ("<p>   </p><p></p>", ""),
    ],
)
def test_converter(html: str, expected: str) -> None:
    assert convert(html) == expected
