import json
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest

from tests.support.site_server import PDF_TEXT, PUBLIC_PAGES, SiteServer, build_pdf


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, Message, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_serves_on_localhost(test_site: SiteServer) -> None:
    assert test_site.base_url.startswith("http://127.0.0.1:")
    assert test_site.url("docs/") == test_site.base_url + "/docs/"


@pytest.mark.parametrize("page", PUBLIC_PAGES)
def test_every_public_page_is_served_as_html(test_site: SiteServer, page: str) -> None:
    status, headers, body = _get(test_site.url(page))
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"<title>" in body


def test_article_has_boilerplate_around_the_main_content(test_site: SiteServer) -> None:
    _, _, body = _get(test_site.url("/article.html"))
    html = body.decode()
    for part in ("<article>", "<nav>", 'class="ads"', "<footer>", 'property="og:title"'):
        assert part in html


def test_js_page_has_no_content_without_javascript(test_site: SiteServer) -> None:
    _, _, body = _get(test_site.url("/js-app.html"))
    html = body.decode()
    assert '<div id="app"></div>' in html
    assert "<noscript>" in html


def test_robots_txt_disallows_private_and_points_to_sitemap(test_site: SiteServer) -> None:
    status, headers, body = _get(test_site.url("/robots.txt"))
    robots = body.decode()
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert "Disallow: /private/" in robots
    assert "Crawl-delay: 1" in robots
    assert f"Sitemap: {test_site.url('/sitemap.xml')}" in robots
    assert _get(test_site.url("/private/staff.html"))[0] == 200  # exists, but is disallowed


def test_sitemap_lists_public_pages_with_absolute_urls(test_site: SiteServer) -> None:
    _, _, body = _get(test_site.url("/sitemap.xml"))
    sitemap = body.decode()
    for page in PUBLIC_PAGES:
        assert f"<loc>{test_site.url(page)}</loc>" in sitemap
    assert "/private/" not in sitemap


def test_slow_route_waits(test_site: SiteServer) -> None:
    started = time.monotonic()
    status, _, _ = _get(test_site.url("/slow?ms=300"))
    assert status == 200
    assert time.monotonic() - started >= 0.3


@pytest.mark.parametrize(("code", "retry_after"), [(429, "2"), (503, "2"), (404, None)])
def test_status_route(test_site: SiteServer, code: int, retry_after: str | None) -> None:
    status, headers, _ = _get(test_site.url(f"/status/{code}"))
    assert status == code
    assert headers.get("Retry-After") == retry_after


def test_retry_after_can_be_set(test_site: SiteServer) -> None:
    _, headers, _ = _get(test_site.url("/status/429?retry_after=30"))
    assert headers["Retry-After"] == "30"


def test_redirect_route(test_site: SiteServer) -> None:
    request = urllib.request.Request(test_site.url("/redirect?to=/docs/"))
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.url == test_site.url("/docs/")


def test_echo_headers_shows_what_the_client_sent(test_site: SiteServer) -> None:
    _, _, body = _get(
        test_site.url("/echo/headers"),
        headers={"User-Agent": "progdata-crawler/test", "Accept-Language": "nb-NO"},
    )
    echoed = json.loads(body)
    assert echoed["user-agent"] == "progdata-crawler/test"
    assert echoed["accept-language"] == "nb-NO"


def test_serves_a_valid_pdf(test_site: SiteServer) -> None:
    status, headers, body = _get(test_site.url("/files/sample.pdf"))
    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert body == build_pdf()
    assert body.startswith(b"%PDF-1.4")
    assert body.rstrip().endswith(b"%%EOF")
    assert PDF_TEXT.encode() in body


def test_pdf_xref_offsets_point_at_objects() -> None:
    pdf = build_pdf()
    xref = pdf[pdf.index(b"\nxref\n") + 1 :].split(b"\n")
    offsets = [int(line[:10]) for line in xref[3:8]]
    for number, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(b"%d 0 obj" % number)
    startxref = int(pdf.split(b"startxref\n")[1].split(b"\n")[0])
    assert pdf[startxref:].startswith(b"xref")


def test_unknown_paths_and_traversal_give_404(test_site: SiteServer) -> None:
    assert _get(test_site.url("/missing.html"))[0] == 404
    assert _get(test_site.url("/../conftest.py"))[0] == 404
    assert _get(test_site.url("/%2e%2e/conftest.py"))[0] == 404


def test_custom_routes_are_served_and_reset(test_site: SiteServer) -> None:
    url = test_site.add(
        "/custom.json", '{"ok": true}', content_type="application/json", headers={"X-Test": "1"}
    )
    status, headers, body = _get(url)
    assert (status, headers["X-Test"], json.loads(body)) == (200, "1", {"ok": True})

    test_site.reset()
    assert _get(url)[0] == 404


def test_custom_routes_override_built_in_ones(test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /\n", content_type="text/plain")
    assert _get(test_site.url("/robots.txt"))[2] == b"User-agent: *\nDisallow: /\n"
