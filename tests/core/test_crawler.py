import base64
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core import crawler as crawler_module
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.models import ErrorCode, FetchRequest, Output, RenderMode, Retention
from progdata_crawler.settings import Settings
from tests.support.documents import build_docx, build_xlsx
from tests.support.site_server import PDF_TEXT, SiteServer

ROBOTS = "User-agent: *\nDisallow: /private/\n"


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", ROBOTS, content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with Crawler(settings, store_path=":memory:") as instance:
        yield instance


def request(url: str, **options: object) -> FetchRequest:
    return FetchRequest.model_validate({"url": url, **options})


async def test_static_page(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/article.html")))
    assert envelope.error is None and envelope.data is not None
    page = envelope.data
    assert page.url == test_site.url("/article.html")
    assert page.markdown is not None and page.markdown.startswith("# How we bake our sourdough")
    assert page.metadata is not None
    assert (page.metadata.title, page.metadata.status) == ("How we bake our sourdough", 200)
    assert page.links is None and page.html is None  # not requested
    assert (envelope.meta.cached, envelope.meta.rendered) == (False, False)
    assert len(envelope.meta.request_id) == 32


async def test_second_fetch_comes_from_cache(crawler: Crawler, test_site: SiteServer) -> None:
    first = await crawler.fetch_envelope(request(test_site.url("/docs/")))
    second = await crawler.fetch_envelope(request(test_site.url("/docs/")))
    assert second.meta.cached is True
    assert second.data == first.data
    fresh = await crawler.fetch_envelope(request(test_site.url("/docs/"), cache_ttl=0))
    assert fresh.meta.cached is False


async def test_fetched_text_is_searchable(crawler: Crawler, test_site: SiteServer) -> None:
    await crawler.fetch_envelope(request(test_site.url("/article.html")))
    hits = await crawler.store.search("stone oven")
    assert [hit.url for hit in hits] == [test_site.url("/article.html")]


async def test_retain_none_stores_nothing(crawler: Crawler, test_site: SiteServer) -> None:
    await crawler.fetch_envelope(request(test_site.url("/article.html"), retain="none"))
    again = await crawler.fetch_envelope(request(test_site.url("/article.html")))
    assert again.meta.cached is False
    assert Retention.NONE.value == "none"


async def test_all_outputs(crawler: Crawler, test_site: SiteServer) -> None:
    outputs = ["markdown", "html", "raw_html", "links", "images", "metadata"]
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/article.html"), outputs=outputs)
    )
    page = envelope.data
    assert page is not None
    assert page.html is not None and page.html.startswith("<article>")
    assert page.raw_html is not None and "<script" not in page.raw_html  # the page has none
    assert page.links is not None and any(
        link.text == "frequently asked questions" for link in page.links
    )
    assert page.images is not None and page.images[0].url == test_site.url("/images/loaf.jpg")


async def test_js_page_is_rendered(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/js-app.html")))
    assert envelope.data is not None, envelope.error
    assert envelope.meta.rendered is True
    assert envelope.data.markdown is not None
    assert "# Order bread" in envelope.data.markdown
    assert "- Sourdough" in envelope.data.markdown


async def test_render_never_keeps_the_plain_html(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/js-app.html"), render=RenderMode.NEVER)
    )
    assert envelope.data is not None
    assert envelope.meta.rendered is False
    assert "Order bread" not in (envelope.data.markdown or "")


async def test_screenshot(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/article.html"), outputs=["markdown", "screenshot"])
    )
    assert envelope.data is not None and envelope.data.screenshot is not None
    assert envelope.meta.rendered is True
    assert base64.b64decode(envelope.data.screenshot).startswith(b"\x89PNG")


async def test_robots_disallowed(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/private/staff.html")))
    assert envelope.data is None and envelope.error is not None
    assert envelope.error.code is ErrorCode.ROBOTS_DISALLOWED


async def test_redirect_into_disallowed_path(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/redirect?to=/private/staff.html"))
    )
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.ROBOTS_DISALLOWED


async def test_blocked_by_site(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/status/403")))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.BLOCKED_BY_SITE


async def test_rate_limited_by_site(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/status/429?retry_after=30")))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.RATE_LIMITED
    assert envelope.error.details["retry_after_s"] == 30
    # The next request to the host is refused at once instead of waiting 30 seconds.
    follow_up = await crawler.fetch_envelope(request(test_site.url("/"), timeout_ms=1_000))
    assert follow_up.error is not None and follow_up.error.code is ErrorCode.RATE_LIMITED


async def test_timeout(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/slow?ms=2000"), timeout_ms=300))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.FETCH_TIMEOUT


async def test_error_status_still_returns_the_page(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/missing.html")))
    assert envelope.data is not None and envelope.data.metadata is not None
    assert envelope.data.metadata.status == 404


async def test_outputs_from_later_phases_are_rejected(
    crawler: Crawler, test_site: SiteServer
) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/"), outputs=["markdown", "structured"])
    )
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.INVALID_REQUEST
    assert envelope.error.details == {"outputs": ["structured"]}


async def test_render_never_with_screenshot_is_rejected(
    crawler: Crawler, test_site: SiteServer
) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/"), outputs=["screenshot"], render="never")
    )
    assert envelope.error is not None and envelope.error.code is ErrorCode.INVALID_REQUEST


async def test_unexpected_errors_become_internal_error(
    crawler: Crawler, test_site: SiteServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(crawler_module, "build_page", broken)
    envelope = await crawler.fetch_envelope(request(test_site.url("/"), cache_ttl=0))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.INTERNAL_ERROR
    assert "bug" not in envelope.error.message


async def test_only_requested_outputs(crawler: Crawler, test_site: SiteServer) -> None:
    envelope = await crawler.fetch_envelope(request(test_site.url("/"), outputs=[Output.LINKS]))
    assert envelope.data is not None
    assert envelope.data.links is not None
    assert envelope.data.markdown is None and envelope.data.metadata is None


async def test_documents_become_markdown(crawler: Crawler, test_site: SiteServer) -> None:
    pdf = await crawler.fetch_envelope(request(test_site.url("/files/sample.pdf")))
    assert pdf.error is None and pdf.data is not None
    assert pdf.data.markdown == PDF_TEXT
    assert pdf.data.metadata is not None
    assert (pdf.data.metadata.status, pdf.data.metadata.content_type) == (200, "application/pdf")

    docx_url = test_site.add(
        "/files/notes.docx", build_docx(), content_type="application/octet-stream"
    )
    docx = await crawler.fetch_envelope(request(docx_url, outputs=["markdown", "links", "html"]))
    assert docx.data is not None and docx.data.markdown is not None
    assert docx.data.markdown.startswith("# Baking notes")
    # Outputs that describe an HTML page stay empty for documents.
    assert (docx.data.links, docx.data.html) == (None, None)

    xlsx_url = test_site.add(
        "/files/recipe",
        build_xlsx(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    xlsx = await crawler.fetch_envelope(request(xlsx_url))
    assert xlsx.data is not None and xlsx.data.markdown is not None
    assert "| Rye | 300 |" in xlsx.data.markdown

    cached = await crawler.fetch_envelope(request(test_site.url("/files/sample.pdf")))
    assert cached.meta.cached and cached.data == pdf.data


async def test_other_files_are_still_unsupported(crawler: Crawler, test_site: SiteServer) -> None:
    image = test_site.add("/files/pixel.png", b"\x89PNG\r\n\x1a\n", content_type="image/png")
    blob = test_site.add("/files/blob", b"\x00\x01\x02", content_type="application/octet-stream")
    for url in (image, blob):
        envelope = await crawler.fetch_envelope(request(url))
        assert envelope.error is not None
        assert envelope.error.code is ErrorCode.UNSUPPORTED_TYPE
    metadata_only = await crawler.fetch_envelope(request(blob, outputs=["metadata"]))
    assert metadata_only.error is not None
