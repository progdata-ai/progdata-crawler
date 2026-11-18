import ipaddress
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch import guard as guard_module
from progdata_crawler.core.fetch.browser import BrowserPool
from progdata_crawler.core.fetch.guard import (
    AddressGuard,
    Resolver,
    is_public_ip,
    resolve_public,
)
from progdata_crawler.core.fetch.http import HttpFetcher
from progdata_crawler.models import ErrorCode, FetchRequest
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer

USER_AGENT = "progdata-crawler/0.0-test (+https://progdata.no/bot)"


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("224.0.0.1", False),  # multicast
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public_ip(address: str, public: bool) -> None:
    assert is_public_ip(ipaddress.ip_address(address)) is public


def resolver(table: dict[str, list[str]]) -> Resolver:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("unknown host")
        return table[host]

    return resolve


async def test_resolve_public() -> None:
    table = {
        "public.test": ["93.184.216.34"],
        "inside.test": ["10.1.2.3"],
        "mixed.test": ["93.184.216.34", "127.0.0.1"],
        "empty.test": [],
    }
    resolve = resolver(table)
    assert await resolve_public("public.test", 443, resolve) == ["93.184.216.34"]
    assert await resolve_public("[2606:2800:220:1::1]", 443, resolve) == ["2606:2800:220:1::1"]
    for host in ("inside.test", "mixed.test", "169.254.169.254", "[::1]"):
        with pytest.raises(CrawlerError) as raised:
            await resolve_public(host, 443, resolve)
        assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED
    for host in ("empty.test", "unknown.test"):
        with pytest.raises(CrawlerError) as raised:
            await resolve_public(host, 443, resolve)
        assert raised.value.code is ErrorCode.FETCH_FAILED


async def test_allow_private_skips_the_check() -> None:
    await AddressGuard(allow_private=True).check_url("http://127.0.0.1/")
    await AddressGuard(allow_private=False).check_url("data:text/plain,hello")  # no host


@pytest.fixture
async def guarded_http() -> AsyncIterator[HttpFetcher]:
    async with HttpFetcher(
        USER_AGENT, timeout_ms=5_000, guard=AddressGuard(allow_private=False)
    ) as http:
        yield http


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/"],
)
async def test_http_refuses_internal_addresses(guarded_http: HttpFetcher, url: str) -> None:
    with pytest.raises(CrawlerError) as raised:
        await guarded_http.fetch(url)
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


async def test_http_refuses_the_local_test_site(
    guarded_http: HttpFetcher, test_site: SiteServer
) -> None:
    with pytest.raises(CrawlerError) as raised:
        await guarded_http.fetch(test_site.url("/"))
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


async def test_http_checks_names_at_connect_time(test_site: SiteServer) -> None:
    """A public-looking name that resolves inside is refused when connecting."""
    guard = AddressGuard(allow_private=False, resolver=resolver({"app.test": ["127.0.0.1"]}))
    port = test_site.base_url.rsplit(":", 1)[1]
    async with HttpFetcher(USER_AGENT, guard=guard) as http:
        with pytest.raises(CrawlerError) as raised:
            await http.fetch(f"http://app.test:{port}/")
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


@pytest.fixture
def only_127_0_0_1_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the test site's address is public, so a redirect away from it can be tested."""

    def fake(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return str(address) == "127.0.0.1"

    monkeypatch.setattr(guard_module, "is_public_ip", fake)


@pytest.mark.usefixtures("only_127_0_0_1_is_public")
async def test_http_checks_every_redirect(guarded_http: HttpFetcher, test_site: SiteServer) -> None:
    port = test_site.base_url.rsplit(":", 1)[1]
    url = test_site.add("/out", "", status=302, headers={"Location": f"http://127.0.0.2:{port}/"})
    assert (await guarded_http.fetch(test_site.url("/"))).status == 200  # "public" is fine
    with pytest.raises(CrawlerError) as raised:
        await guarded_http.fetch(url)
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def guarded_browser() -> AsyncIterator[BrowserPool]:
    settings = Settings()
    async with BrowserPool(
        USER_AGENT,
        executable_path=settings.browser_executable,
        guard=AddressGuard(allow_private=False),
    ) as pool:
        yield pool


@pytest.mark.asyncio(loop_scope="module")
async def test_browser_refuses_internal_addresses(
    guarded_browser: BrowserPool, test_site: SiteServer
) -> None:
    with pytest.raises(CrawlerError) as raised:
        await guarded_browser.render(test_site.url("/js-app.html"))
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.usefixtures("only_127_0_0_1_is_public")
async def test_browser_checks_redirects(
    guarded_browser: BrowserPool, test_site: SiteServer
) -> None:
    port = test_site.base_url.rsplit(":", 1)[1]
    url = test_site.add("/out", "", status=302, headers={"Location": f"http://127.0.0.2:{port}/"})
    assert "Fjordline" in (await guarded_browser.render(test_site.url("/"))).html
    with pytest.raises(CrawlerError) as raised:
        await guarded_browser.render(url)
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


async def test_crawler_is_protected_unless_told_otherwise(
    tmp_path: Path, test_site: SiteServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDC_ALLOW_PRIVATE_NETWORKS")
    settings = Settings(data_dir=tmp_path)
    assert settings.allow_private_networks is None
    async with Crawler(settings, store_path=":memory:") as crawler:
        envelope = await crawler.fetch_envelope(
            FetchRequest.model_validate({"url": test_site.url("/")})
        )
    assert envelope.error is not None
    assert envelope.error.code is ErrorCode.ADDRESS_NOT_ALLOWED
