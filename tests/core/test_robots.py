import asyncio
import urllib.error
import urllib.request

import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.polite.robots import (
    RobotsFile,
    RobotsPolicy,
    RobotsRules,
    origin_of,
)
from progdata_crawler.models import ErrorCode
from tests.support.site_server import SiteServer

ROBOTS = """
User-agent: *
Disallow: /private/
Crawl-delay: 2

User-agent: progdata-crawler
Disallow: /no-crawlers/
Crawl-delay: 0.5

Sitemap: https://example.com/sitemap.xml
"""


class FakeFetcher:
    """Serves robots.txt responses per origin and counts requests."""

    def __init__(self, responses: dict[str, RobotsFile | Exception]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    async def __call__(self, url: str) -> RobotsFile:
        self.requests.append(url)
        await asyncio.sleep(0)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def policy_for(response: RobotsFile | Exception) -> RobotsPolicy:
    return RobotsPolicy(FakeFetcher({"https://example.com/robots.txt": response}))


async def test_most_specific_group_applies_to_us() -> None:
    policy = policy_for(RobotsFile(200, ROBOTS))
    await policy.check("https://example.com/private/page")  # only `*` disallows this
    with pytest.raises(CrawlerError) as raised:
        await policy.check("https://example.com/no-crawlers/page")
    assert raised.value.code is ErrorCode.ROBOTS_DISALLOWED
    assert raised.value.details == {"url": "https://example.com/no-crawlers/page"}
    assert await policy.crawl_delay("https://example.com/") == 0.5


def test_rules_expose_sitemaps() -> None:
    assert RobotsRules.parse(ROBOTS).sitemaps() == ["https://example.com/sitemap.xml"]


@pytest.mark.parametrize("status", [404, 401, 403, 410])
async def test_client_errors_mean_no_rules(status: int) -> None:
    policy = policy_for(RobotsFile(status, "<html>not found</html>"))
    await policy.check("https://example.com/anything")
    assert await policy.crawl_delay("https://example.com/") is None


@pytest.mark.parametrize(
    "response",
    [RobotsFile(500, ""), RobotsFile(503, ""), RobotsFile(429, ""), ConnectionError("refused")],
)
async def test_unavailable_robots_disallows_everything(response: RobotsFile | Exception) -> None:
    policy = policy_for(response)
    with pytest.raises(CrawlerError):
        await policy.check("https://example.com/")


async def test_malformed_robots_is_read_leniently() -> None:
    policy = policy_for(RobotsFile(200, "this is not robots.txt\n<<<>>>\nDisallow: /x\n"))
    await policy.check("https://example.com/allowed")


async def test_empty_robots_allows_everything() -> None:
    policy = policy_for(RobotsFile(200, ""))
    await policy.check("https://example.com/private/page")


async def test_fetches_once_per_origin_even_when_concurrent() -> None:
    fetcher = FakeFetcher(
        {
            "https://example.com/robots.txt": RobotsFile(200, ROBOTS),
            "https://example.com:8443/robots.txt": RobotsFile(404, ""),
        }
    )
    policy = RobotsPolicy(fetcher)
    await asyncio.gather(*(policy.check(f"https://example.com/page-{i}") for i in range(10)))
    await policy.check("https://EXAMPLE.com/other")
    await policy.check("https://example.com:8443/no-crawlers/")
    assert fetcher.requests == [
        "https://example.com/robots.txt",
        "https://example.com:8443/robots.txt",
    ]


async def test_cache_expires() -> None:
    clock = FakeClock()
    fetcher = FakeFetcher({"https://example.com/robots.txt": RobotsFile(200, "")})
    policy = RobotsPolicy(fetcher, ttl_s=100, clock=clock)
    await policy.check("https://example.com/")
    clock.now += 99
    await policy.check("https://example.com/")
    clock.now += 2
    await policy.check("https://example.com/")
    assert len(fetcher.requests) == 2


async def test_unavailable_robots_is_retried_sooner() -> None:
    clock = FakeClock()
    fetcher = FakeFetcher({"https://example.com/robots.txt": RobotsFile(503, "")})
    policy = RobotsPolicy(fetcher, ttl_s=3600, unavailable_ttl_s=60, clock=clock)
    with pytest.raises(CrawlerError):
        await policy.check("https://example.com/")

    fetcher.responses["https://example.com/robots.txt"] = RobotsFile(200, "")
    clock.now += 61
    await policy.check("https://example.com/")
    assert len(fetcher.requests) == 2


def test_origin_of() -> None:
    assert origin_of("HTTPS://Example.COM/a/b?c=d") == "https://example.com"
    assert origin_of("http://example.com:8080/") == "http://example.com:8080"
    with pytest.raises(ValueError, match="not an http"):
        origin_of("ftp://example.com/")


async def test_against_the_local_test_site(test_site: SiteServer) -> None:
    async def fetch(url: str) -> RobotsFile:
        def get() -> RobotsFile:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return RobotsFile(response.status, response.read().decode())
            except urllib.error.HTTPError as error:
                return RobotsFile(error.code, "")

        return await asyncio.to_thread(get)

    policy = RobotsPolicy(fetch)
    await policy.check(test_site.url("/article.html"))
    with pytest.raises(CrawlerError):
        await policy.check(test_site.url("/private/staff.html"))
    assert await policy.crawl_delay(test_site.url("/")) == 1.0
    rules = await policy.rules_for(test_site.url("/"))
    assert rules.sitemaps() == [test_site.url("/sitemap.xml")]
