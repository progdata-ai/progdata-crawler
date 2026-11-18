import gzip
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover, discover_envelope
from progdata_crawler.core.discover.discover import in_scope
from progdata_crawler.models import DiscoverRequest, ErrorCode, Scope, UrlSource
from progdata_crawler.settings import Settings
from tests.support.site_server import PUBLIC_PAGES, SiteServer


def robots(test_site: SiteServer, *sitemaps: str) -> None:
    lines = ["User-agent: *", "Disallow: /private/", ""]
    lines += [f"Sitemap: {test_site.url(path)}" for path in sitemaps]
    test_site.add("/robots.txt", "\n".join(lines) + "\n", content_type="text/plain")


def urlset(test_site: SiteServer, *paths: str) -> str:
    entries = "".join(f"<url><loc>{test_site.url(path)}</loc></url>" for path in paths)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'


@pytest.fixture
async def crawler(tmp_path: Path) -> AsyncIterator[Crawler]:
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with Crawler(settings, store_path=":memory:") as instance:
        yield instance


def request(url: str, **options: object) -> DiscoverRequest:
    return DiscoverRequest.model_validate({"url": url, **options})


async def test_sitemap_from_robots_and_start_page_links(
    crawler: Crawler, test_site: SiteServer
) -> None:
    robots(test_site, "/sitemap.xml")
    result = await discover(crawler, request(test_site.url("/")))

    urls = {item.url: item for item in result.urls}
    assert set(urls) == {test_site.url(path) for path in PUBLIC_PAGES}
    assert result.sitemaps == (test_site.url("/sitemap.xml"),)
    assert urls[test_site.url("/docs/faq.html")].source is UrlSource.SITEMAP
    # Found in the sitemap first, the link text is added from the start page.
    assert urls[test_site.url("/article.html")].text != ""
    # Disallowed by robots.txt, and on another host.
    assert test_site.url("/private/staff.html") not in urls
    assert not result.truncated


async def test_falls_back_to_sitemap_xml(crawler: Crawler, test_site: SiteServer) -> None:
    robots(test_site)
    result = await discover(crawler, request(test_site.url("/"), links=False))
    assert result.sitemaps == (test_site.url("/sitemap.xml"),)
    assert len(result.urls) == len(PUBLIC_PAGES)


async def test_sitemap_index_and_gzip(crawler: Crawler, test_site: SiteServer) -> None:
    robots(test_site, "/maps/index.xml")
    test_site.add(
        "/maps/index.xml",
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{test_site.url('/maps/one.xml')}</loc></sitemap>"
        f"<sitemap><loc>{test_site.url('/maps/two.xml.gz')}</loc></sitemap>"
        f"<sitemap><loc>{test_site.url('/maps/index.xml')}</loc></sitemap>"
        f"<sitemap><loc>{test_site.url('/maps/missing.xml')}</loc></sitemap>"
        "</sitemapindex>",
        content_type="application/xml",
    )
    test_site.add("/maps/one.xml", urlset(test_site, "/a", "/b"), content_type="application/xml")
    test_site.add(
        "/maps/two.xml.gz",
        gzip.compress(urlset(test_site, "/c", "/a").encode()),
        content_type="application/gzip",
    )
    result = await discover(crawler, request(test_site.url("/"), links=False))
    assert [item.url for item in result.urls] == [test_site.url(p) for p in ("/a", "/b", "/c")]
    assert len(result.sitemaps) == 3  # the missing one is skipped, the loop is not followed


async def test_filters(crawler: Crawler, test_site: SiteServer) -> None:
    robots(test_site, "/sitemap.xml")
    docs = await discover(
        crawler,
        request(test_site.url("/"), paths={"include": ["^/docs/"], "exclude": ["faq"]}),
    )
    assert {item.url for item in docs.urls} == {
        test_site.url(path)
        for path in ("/docs/", "/docs/getting-started.html", "/docs/configuration.html")
    }

    found = await discover(crawler, request(test_site.url("/"), query="CONFIGURATION"))
    assert [item.url for item in found.urls] == [test_site.url("/docs/configuration.html")]


async def test_limit(crawler: Crawler, test_site: SiteServer) -> None:
    robots(test_site, "/sitemap.xml")
    result = await discover(crawler, request(test_site.url("/"), limit=2))
    assert len(result.urls) == 2
    assert result.truncated


async def test_external_links_only_with_external_scope(
    crawler: Crawler, test_site: SiteServer
) -> None:
    robots(test_site)
    start = test_site.url("/")
    site = await discover(crawler, request(start, sitemaps=False))
    external = await discover(crawler, request(start, sitemaps=False, scope=Scope.EXTERNAL))
    assert all("external.invalid" not in item.url for item in site.urls)
    assert any("external.invalid" in item.url for item in external.urls)


def test_scope() -> None:
    start = "https://www.example.test/"
    assert in_scope("https://example.test/a", start, Scope.SITE)
    assert not in_scope("https://docs.example.test/a", start, Scope.SITE)
    assert in_scope("https://docs.example.test/a", start, Scope.SUBDOMAINS)
    assert not in_scope("https://notexample.test/a", start, Scope.SUBDOMAINS)
    assert in_scope("https://other.test/a", start, Scope.EXTERNAL)


async def test_unreachable_site_is_an_error(crawler: Crawler, test_site: SiteServer) -> None:
    robots(test_site)
    test_site.add("/sitemap.xml", "nope", status=404)
    envelope = await discover_envelope(crawler, request(test_site.url("/status/403")))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.BLOCKED_BY_SITE


async def test_disallowed_start_page(crawler: Crawler, test_site: SiteServer) -> None:
    robots(test_site)
    test_site.add("/sitemap.xml", "nope", status=404)
    envelope = await discover_envelope(crawler, request(test_site.url("/private/")))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.ROBOTS_DISALLOWED


async def test_internal_addresses_are_refused(tmp_path: Path, test_site: SiteServer) -> None:
    settings = Settings(data_dir=tmp_path, allow_private_networks=False)
    async with Crawler(settings, store_path=":memory:") as crawler:
        envelope = await discover_envelope(crawler, request(test_site.url("/")))
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.ADDRESS_NOT_ALLOWED
