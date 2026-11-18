import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.detect import (
    check_render_options,
    looks_like_js_shell,
    needs_browser_features,
    should_render_after,
    should_render_first,
)
from progdata_crawler.models import ErrorCode, Output, PageOptions, RenderMode
from tests.support.site_server import STATIC_DIR

PROSE = "<p>" + "Fresh bread every morning, baked in a stone oven by the fjord. " * 5 + "</p>"


def page(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("js-app.html", True),
        ("article.html", False),
        ("index.html", False),
        ("docs/faq.html", False),
    ],
)
def test_test_site_pages(name: str, expected: bool) -> None:
    html = (STATIC_DIR / name).read_text()
    assert looks_like_js_shell(html) is expected


@pytest.mark.parametrize(
    "html",
    [
        page('<div id="root"></div><script src="/bundle.js"></script>'),
        page('<div id="__next">  </div>'),
        page("<div data-reactroot></div>"),
        page("<app-root ng-version='17.0.0'></app-root>"),
        page("<noscript>You need to enable JavaScript to run this app.</noscript>"),
        page("<p>Loading…</p><script>start()</script>"),
    ],
)
def test_detects_shells(html: str) -> None:
    assert looks_like_js_shell(html)


@pytest.mark.parametrize(
    "html",
    [
        page(PROSE + '<div id="root"></div><script>widgets()</script>'),  # enough text
        page('<div id="app"><h1>Server-rendered</h1></div>'),  # mount point has content
        page("<p>A short static page.</p>"),  # little text, but no sign of JS
        page(""),
        "",
        "not html at all",
    ],
)
def test_ignores_real_pages(html: str) -> None:
    assert not looks_like_js_shell(html)


def test_text_in_scripts_and_styles_does_not_count() -> None:
    hidden = "x" * 500
    html = page(f'<div id="app"></div><script>var s="{hidden}"</script><style>/*{hidden}*/</style>')
    assert looks_like_js_shell(html)


def test_browser_features() -> None:
    assert not needs_browser_features(PageOptions())
    assert needs_browser_features(PageOptions(outputs=(Output.MARKDOWN, Output.SCREENSHOT)))
    assert needs_browser_features(PageOptions(wait_ms=500))


@pytest.mark.parametrize(
    ("options", "first", "after_static", "after_shell"),
    [
        (PageOptions(), False, False, True),
        (PageOptions(render=RenderMode.ALWAYS), True, True, True),
        (PageOptions(render=RenderMode.NEVER), False, False, False),
        (PageOptions(wait_ms=100), True, True, True),
    ],
)
def test_render_decisions(
    options: PageOptions, first: bool, after_static: bool, after_shell: bool
) -> None:
    static = (STATIC_DIR / "article.html").read_text()
    shell = (STATIC_DIR / "js-app.html").read_text()
    assert should_render_first(options) is first
    assert should_render_after(options, static) is after_static
    assert should_render_after(options, shell) is after_shell


def test_never_render_with_browser_features_is_rejected() -> None:
    check_render_options(PageOptions(render=RenderMode.NEVER))
    with pytest.raises(CrawlerError) as raised:
        check_render_options(PageOptions(render=RenderMode.NEVER, wait_ms=100))
    assert raised.value.code is ErrorCode.INVALID_REQUEST
