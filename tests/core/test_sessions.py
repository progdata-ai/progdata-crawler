from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.session.browser_session import SessionManager
from progdata_crawler.models import ErrorCode, SessionOpenRequest, SessionStepRequest
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer

THANKS = (
    "<html><head><title>Thanks</title></head><body><h1>Thank you for your order</h1></body></html>"
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    test_site.add("/submit", THANKS)
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with Crawler(settings, store_path=":memory:") as instance:
        yield instance


def open_request(url: str, **options: Any) -> SessionOpenRequest:
    return SessionOpenRequest.model_validate({"url": url, **options})


def steps(*items: dict[str, Any]) -> SessionStepRequest:
    return SessionStepRequest.model_validate({"steps": list(items)})


async def test_steps_across_several_calls(crawler: Crawler, test_site: SiteServer) -> None:
    async with SessionManager(crawler) as sessions:
        opened = await sessions.open(open_request(test_site.url("/form.html")))
        assert opened.page is not None and opened.page.markdown is not None
        assert "Order bread" in opened.page.markdown

        typed = await sessions.step(
            opened.id, steps({"action": "type", "selector": "#name", "text": "Ada"})
        )
        assert typed.page is not None and typed.page.url == test_site.url("/form.html")

        sent = await sessions.step(opened.id, steps({"action": "click", "selector": "#send"}))
        assert sent.page is not None and sent.page.url == test_site.url("/submit")
        assert sent.page.markdown is not None and "Thank you" in sent.page.markdown

        closed = await sessions.close(opened.id)
        assert closed.closed and closed.page is None

    received = [r for r in test_site.received if r.path == "/submit"]
    assert parse_qs(received[0].body.decode())["name"] == ["Ada"]


COOKIE_CHECK = (
    '<html><body><p id="jar">?</p><script>'
    'document.getElementById("jar").textContent = "cookies: " + document.cookie;'
    "</script></body></html>"
)


async def test_sessions_do_not_share_cookies(crawler: Crawler, test_site: SiteServer) -> None:
    test_site.add(
        "/cookie",
        '<html><body><a id="next" href="/cookie-check">next</a></body></html>',
        headers={"Set-Cookie": "basket=rye; Path=/"},
    )
    test_site.add("/cookie-check", COOKIE_CHECK)
    options = {"content": "full"}
    async with SessionManager(crawler) as sessions:
        first = await sessions.open(open_request(test_site.url("/cookie"), **options))
        followed = await sessions.step(first.id, steps({"action": "click", "selector": "#next"}))
        second = await sessions.open(open_request(test_site.url("/cookie-check"), **options))
    assert followed.page is not None and followed.page.markdown is not None
    assert "cookies: basket=rye" in followed.page.markdown
    assert second.page is not None and second.page.markdown is not None
    assert "basket" not in second.page.markdown


async def test_idle_sessions_are_closed(crawler: Crawler, test_site: SiteServer) -> None:
    clock = Clock()
    async with SessionManager(crawler, ttl_s=60, max_age_s=600, clock=clock) as sessions:
        opened = await sessions.open(open_request(test_site.url("/form.html")))
        assert opened.expires_at == clock.now + 60
        clock.now += 30
        await sessions.step(opened.id, steps())  # use keeps it alive
        clock.now += 50
        assert await sessions.sweep() == 0
        clock.now += 20
        assert await sessions.sweep() == 1
        assert sessions.open_count == 0
        with pytest.raises(CrawlerError) as raised:
            await sessions.step(opened.id, steps())
        assert raised.value.code is ErrorCode.NOT_FOUND


async def test_maximum_age_applies_even_when_used(crawler: Crawler, test_site: SiteServer) -> None:
    clock = Clock()
    async with SessionManager(crawler, ttl_s=60, max_age_s=100, clock=clock) as sessions:
        opened = await sessions.open(open_request(test_site.url("/form.html")))
        for _ in range(3):
            clock.now += 40
            if clock.now - 1_800_000_000.0 < 100:
                await sessions.step(opened.id, steps())
        assert await sessions.sweep() == 1


async def test_limit_on_open_sessions(crawler: Crawler, test_site: SiteServer) -> None:
    async with SessionManager(crawler, max_sessions=1) as sessions:
        first = await sessions.open(open_request(test_site.url("/form.html")))
        with pytest.raises(CrawlerError) as raised:
            await sessions.open(open_request(test_site.url("/form.html")))
        assert raised.value.code is ErrorCode.RATE_LIMITED
        await sessions.close(first.id)
        await sessions.open(open_request(test_site.url("/form.html")))


async def test_robots_txt_applies_in_sessions(crawler: Crawler, test_site: SiteServer) -> None:
    async with SessionManager(crawler) as sessions:
        with pytest.raises(CrawlerError) as refused_open:
            await sessions.open(open_request(test_site.url("/private/staff.html")))
        opened = await sessions.open(open_request(test_site.url("/form.html")))
        with pytest.raises(CrawlerError) as refused_step:
            await sessions.step(opened.id, steps({"action": "click", "selector": "#staff"}))
    assert refused_open.value.code is ErrorCode.ROBOTS_DISALLOWED
    assert refused_step.value.code is ErrorCode.ROBOTS_DISALLOWED


async def test_unknown_and_closed_sessions(crawler: Crawler, test_site: SiteServer) -> None:
    async with SessionManager(crawler) as sessions:
        opened = await sessions.open(open_request(test_site.url("/form.html")))
        await sessions.close(opened.id)
        for call in (
            sessions.step(opened.id, steps()),
            sessions.close(opened.id),
            sessions.close("nope"),
        ):
            with pytest.raises(CrawlerError) as raised:
                await call
            assert raised.value.code is ErrorCode.NOT_FOUND


async def test_closing_the_manager_closes_every_session(
    crawler: Crawler, test_site: SiteServer
) -> None:
    sessions = SessionManager(crawler)
    async with sessions:
        await sessions.open(open_request(test_site.url("/form.html")))
        await sessions.open(open_request(test_site.url("/form.html")))
        assert sessions.open_count == 2
    assert sessions.open_count == 0
