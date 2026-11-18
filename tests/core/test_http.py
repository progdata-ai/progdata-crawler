import json
import socket
from collections.abc import AsyncIterator

import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.http import HttpFetcher, HttpResponse, raise_for_blocked
from progdata_crawler.core.polite.robots import RobotsPolicy
from progdata_crawler.models import ErrorCode
from tests.support.site_server import SiteServer

USER_AGENT = "progdata-crawler/0.0-test (+https://progdata.no/bot)"


@pytest.fixture
async def fetcher() -> AsyncIterator[HttpFetcher]:
    async with HttpFetcher(USER_AGENT, timeout_ms=5_000) as http:
        yield http


async def test_fetches_html(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    response = await fetcher.fetch(test_site.url("/article.html"))
    assert response.status == 200
    assert response.url == test_site.url("/article.html")
    assert response.content_type == "text/html"
    assert response.charset == "utf-8"
    assert "<h1>How we bake our sourdough</h1>" in response.text()
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.elapsed_ms >= 0


async def test_sends_honest_user_agent_and_locale(
    fetcher: HttpFetcher, test_site: SiteServer
) -> None:
    response = await fetcher.fetch(
        test_site.url("/echo/headers"), locale="nb-NO", accept_types={"application/json"}
    )
    headers = json.loads(response.text())
    assert headers["user-agent"] == USER_AGENT
    assert headers["accept-language"] == "nb-NO"


async def test_follows_redirects(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    response = await fetcher.fetch(test_site.url("/redirect?to=/docs/"))
    assert response.url == test_site.url("/docs/")
    assert response.redirects == (test_site.url("/redirect?to=/docs/"),)


async def test_error_statuses_are_returned(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    response = await fetcher.fetch(test_site.url("/missing.html"))
    assert response.status == 404
    raise_for_blocked(response)  # 404 is not a block


@pytest.mark.parametrize("status", [401, 403, 451])
async def test_blocked_statuses(fetcher: HttpFetcher, test_site: SiteServer, status: int) -> None:
    response = await fetcher.fetch(test_site.url(f"/status/{status}"))
    with pytest.raises(CrawlerError) as raised:
        raise_for_blocked(response)
    assert raised.value.code is ErrorCode.BLOCKED_BY_SITE
    assert raised.value.details["status"] == status


async def test_timeout(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    with pytest.raises(CrawlerError) as raised:
        await fetcher.fetch(test_site.url("/slow?ms=2000"), timeout_ms=200)
    assert raised.value.code is ErrorCode.FETCH_TIMEOUT
    assert "200 ms" in raised.value.message


async def test_unsupported_content_type(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    with pytest.raises(CrawlerError) as raised:
        await fetcher.fetch(test_site.url("/files/sample.pdf"))
    assert raised.value.code is ErrorCode.UNSUPPORTED_TYPE
    assert raised.value.details["content_type"] == "application/pdf"


async def test_any_type_when_accept_types_is_none(
    fetcher: HttpFetcher, test_site: SiteServer
) -> None:
    response = await fetcher.fetch(test_site.url("/files/sample.pdf"), accept_types=None)
    assert response.content.startswith(b"%PDF")


async def test_body_size_limit(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    url = test_site.add("/big.html", "<p>" + "x" * 5_000 + "</p>")
    assert len((await fetcher.fetch(url, max_bytes=10_000)).content) > 5_000
    with pytest.raises(CrawlerError) as raised:
        await fetcher.fetch(url, max_bytes=1_000)
    assert raised.value.code is ErrorCode.UNSUPPORTED_TYPE
    assert raised.value.details["max_bytes"] == 1_000


async def test_network_error(fetcher: HttpFetcher) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(CrawlerError) as raised:
        await fetcher.fetch(f"http://127.0.0.1:{port}/")
    assert raised.value.code is ErrorCode.FETCH_FAILED


async def test_too_many_redirects(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    test_site.add("/loop", "", status=302, headers={"Location": test_site.url("/loop")})
    with pytest.raises(CrawlerError) as raised:
        await fetcher.fetch(test_site.url("/loop"))
    assert raised.value.code is ErrorCode.FETCH_FAILED
    assert raised.value.message == "too many redirects"


async def test_decodes_declared_charset(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    url = test_site.add(
        "/latin1.html",
        "<p>blåbær</p>".encode("latin-1"),
        content_type="text/html; charset=ISO-8859-1",
    )
    response = await fetcher.fetch(url)
    assert response.charset == "iso-8859-1"
    assert "blåbær" in response.text()


def test_unknown_charset_falls_back_to_utf8() -> None:
    response = HttpResponse(
        url="https://example.com/",
        status=200,
        headers={},
        content="æøå".encode(),
        content_type="text/html",
        charset="no-such-charset",
        elapsed_ms=1,
    )
    assert response.text() == "æøå"


async def test_works_as_the_robots_fetcher(fetcher: HttpFetcher, test_site: SiteServer) -> None:
    policy = RobotsPolicy(fetcher.fetch_robots)
    await policy.check(test_site.url("/article.html"))
    with pytest.raises(CrawlerError) as raised:
        await policy.check(test_site.url("/private/staff.html"))
    assert raised.value.code is ErrorCode.ROBOTS_DISALLOWED
