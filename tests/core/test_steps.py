from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.models import ErrorCode, FetchRequest
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer

THANKS = (
    "<html><head><title>Thanks</title></head><body><h1>Thank you for your order</h1></body></html>"
)


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    test_site.add("/submit", THANKS)
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with Crawler(settings, store_path=":memory:") as instance:
        yield instance


def request(url: str, steps: list[dict[str, Any]], **options: Any) -> FetchRequest:
    return FetchRequest.model_validate({"url": url, "steps": steps, **options})


async def test_fill_in_and_send_a_form(crawler: Crawler, test_site: SiteServer) -> None:
    steps = [
        {"action": "type", "selector": "#name", "text": "Ada"},
        {"action": "type", "selector": "#loaves", "text": "3"},
        {"action": "capture"},
        {"action": "click", "selector": "#send"},
    ]
    envelope = await crawler.fetch_envelope(request(test_site.url("/form.html"), steps))
    assert envelope.error is None and envelope.data is not None
    page = envelope.data
    assert page.url == test_site.url("/submit")
    assert page.markdown is not None and page.markdown.startswith("# Thank you for your order")
    assert envelope.meta.rendered

    sent = [r for r in test_site.received if r.path == "/submit"]
    assert len(sent) == 1
    assert parse_qs(sent[0].body.decode()) == {"name": ["Ada"], "loaves": ["3"]}

    assert page.captures is not None and len(page.captures) == 1
    capture = page.captures[0]
    assert (capture.step, capture.url) == (3, test_site.url("/form.html"))
    assert capture.markdown is not None and "Order bread" in capture.markdown


async def test_click_and_wait_for_content(crawler: Crawler, test_site: SiteServer) -> None:
    steps = [
        {"action": "click", "selector": "#more"},
        {"action": "wait", "selector": "#hours"},
        {"action": "scroll", "direction": "down", "pixels": 400},
        {"action": "press", "key": "Tab"},
    ]
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/form.html"), steps, content="full")
    )
    assert envelope.error is None and envelope.data is not None
    assert envelope.data.markdown is not None
    assert "Open every day from 7 to 15." in envelope.data.markdown


async def test_missing_element_names_the_step(crawler: Crawler, test_site: SiteServer) -> None:
    steps = [
        {"action": "type", "selector": "#name", "text": "Ada"},
        {"action": "click", "selector": "#does-not-exist", "timeout_ms": 300},
    ]
    envelope = await crawler.fetch_envelope(request(test_site.url("/form.html"), steps))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.STEP_FAILED
    assert envelope.error.details["step"] == 2
    assert "'#does-not-exist'" in envelope.error.message


async def test_navigation_to_a_disallowed_page_is_refused(
    crawler: Crawler, test_site: SiteServer
) -> None:
    steps = [{"action": "click", "selector": "#staff"}]
    envelope = await crawler.fetch_envelope(request(test_site.url("/form.html"), steps))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.ROBOTS_DISALLOWED


async def test_allowed_navigation_follows_the_link(crawler: Crawler, test_site: SiteServer) -> None:
    steps = [{"action": "click", "selector": "#article"}]
    envelope = await crawler.fetch_envelope(request(test_site.url("/form.html"), steps))
    assert envelope.data is not None, envelope.error
    assert envelope.data.url == test_site.url("/article.html")
    assert envelope.data.markdown is not None
    assert envelope.data.markdown.startswith("# How we bake our sourdough")


async def test_pages_with_steps_are_not_cached(crawler: Crawler, test_site: SiteServer) -> None:
    steps = [{"action": "click", "selector": "#send"}]
    first = await crawler.fetch_envelope(request(test_site.url("/form.html"), steps))
    second = await crawler.fetch_envelope(request(test_site.url("/form.html"), steps))
    assert not first.meta.cached and not second.meta.cached
    assert len([r for r in test_site.received if r.path == "/submit"]) == 2


async def test_steps_need_a_browser(crawler: Crawler, test_site: SiteServer) -> None:
    steps = [{"action": "click", "selector": "#send"}]
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/form.html"), steps, render="never")
    )
    assert envelope.error is not None and envelope.error.code is ErrorCode.INVALID_REQUEST


def test_step_validation() -> None:
    with pytest.raises(ValueError):
        request("https://example.test/", [{"action": "fly"}])
    with pytest.raises(ValueError):
        request("https://example.test/", [{"action": "click"}])
    with pytest.raises(ValueError):
        request("https://example.test/", [{"action": "capture"}] * 51)
