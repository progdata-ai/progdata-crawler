import json
import struct
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.browser import BrowserPool, mobile_user_agent
from progdata_crawler.models import Device, ErrorCode
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer

USER_AGENT = "progdata-crawler/0.0-test (+https://progdata.no/bot)"

# One browser for the whole module: tests share its event loop.
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pool() -> AsyncIterator[BrowserPool]:
    settings = Settings()
    async with BrowserPool(
        USER_AGENT, max_pages=2, executable_path=settings.browser_executable
    ) as browser_pool:
        yield browser_pool


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


async def test_renders_javascript(pool: BrowserPool, test_site: SiteServer) -> None:
    page = await pool.render(test_site.url("/js-app.html"))
    assert page.status == 200
    assert page.url == test_site.url("/js-app.html")
    assert page.content_type == "text/html"
    assert "<h1>Order bread</h1>" in page.html
    assert "<li>Sourdough</li>" in page.html
    assert page.screenshot is None


async def test_reuses_contexts(pool: BrowserPool, test_site: SiteServer) -> None:
    await pool.render(test_site.url("/"))
    created = pool.contexts_created
    for _ in range(3):
        await pool.render(test_site.url("/docs/"))
    assert pool.contexts_created == created


async def test_cookies_do_not_leak_between_pages(pool: BrowserPool, test_site: SiteServer) -> None:
    url = test_site.add(
        "/cookie.html",
        "<body><p id=out></p><script>"
        "document.getElementById('out').textContent = document.cookie || 'none';"
        "document.cookie = 'seen=yes';"
        "</script></body>",
    )
    first = await pool.render(url)
    second = await pool.render(url)
    assert '<p id="out">none</p>' in first.html
    assert '<p id="out">none</p>' in second.html


async def test_sends_honest_user_agent_and_locale(pool: BrowserPool, test_site: SiteServer) -> None:
    page = await pool.render(test_site.url("/echo/headers"), locale="nb-NO", accept_types=None)
    body = page.html[page.html.index("{") : page.html.rindex("}") + 1]
    headers = json.loads(body)
    assert headers["user-agent"] == USER_AGENT
    assert headers["accept-language"].startswith("nb-NO")


async def test_mobile_device(pool: BrowserPool, test_site: SiteServer) -> None:
    url = test_site.add(
        "/viewport.html",
        '<head><meta name="viewport" content="width=device-width"></head>'
        "<body><p id=w></p><script>"
        "document.getElementById('w').textContent = window.innerWidth + ' ' + navigator.userAgent;"
        "</script></body>",
    )
    desktop = await pool.render(url)
    mobile = await pool.render(url, device=Device.MOBILE)
    assert f">1280 {USER_AGENT}<" in desktop.html
    assert f">390 {mobile_user_agent(USER_AGENT)}<" in mobile.html


async def test_wait_ms_lets_late_content_appear(pool: BrowserPool, test_site: SiteServer) -> None:
    url = test_site.add(
        "/late.html",
        "<body><div id=late></div><script>"
        "setTimeout(() => { document.getElementById('late').textContent = 'arrived'; }, 1500);"
        "</script></body>",
    )
    assert '<div id="late"></div>' in (await pool.render(url)).html
    assert '<div id="late">arrived</div>' in (await pool.render(url, wait_ms=3000)).html


async def test_screenshots(pool: BrowserPool, test_site: SiteServer) -> None:
    url = test_site.add(
        "/tall.html", "<body style='margin:0'><div style='height:3000px'>Tall</div></body>"
    )
    visible = await pool.render(url, screenshot=True)
    full = await pool.render(url, screenshot=True, full_page=True)
    assert visible.screenshot is not None and full.screenshot is not None
    assert png_size(visible.screenshot) == (1280, 800)
    assert png_size(full.screenshot) == (1280, 3000)


async def test_timeout(pool: BrowserPool, test_site: SiteServer) -> None:
    with pytest.raises(CrawlerError) as raised:
        await pool.render(test_site.url("/slow?ms=3000"), timeout_ms=500)
    assert raised.value.code is ErrorCode.FETCH_TIMEOUT
    # The pool still works afterwards.
    assert "Fjordline" in (await pool.render(test_site.url("/"))).html


async def test_unsupported_type(pool: BrowserPool, test_site: SiteServer) -> None:
    url = test_site.add("/data.json", '{"a": 1}', content_type="application/json")
    with pytest.raises(CrawlerError) as raised:
        await pool.render(url)
    assert raised.value.code is ErrorCode.UNSUPPORTED_TYPE


async def test_error_status_is_reported(pool: BrowserPool, test_site: SiteServer) -> None:
    page = await pool.render(test_site.url("/missing.html"))
    assert page.status == 404


async def test_network_error(pool: BrowserPool) -> None:
    with pytest.raises(CrawlerError) as raised:
        await pool.render("http://127.0.0.1:9/")
    assert raised.value.code is ErrorCode.FETCH_FAILED


async def test_mobile_user_agent() -> None:
    assert mobile_user_agent("x/1 (+https://a)") == "x/1 (+https://a; mobile)"
    assert mobile_user_agent("x/1") == "x/1 (mobile)"
