"""Several processes share the data directory (serve, an MCP server per Claude client,
pdc watch). Each due watch must be checked by exactly one of them."""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.jobs.notify import WebhookNotifier
from progdata_crawler.core.watch.scheduler import CLAIM_LEASE_S, WatchService
from progdata_crawler.core.watch.store import WatchStore
from progdata_crawler.models.watch import WatchCreate
from progdata_crawler.settings import Settings, default_user_agent
from tests.support.site_server import SiteServer


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    async with Crawler(Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)) as instance:
        yield instance


def service(crawler: Crawler, path: Path, clock: Clock) -> WatchService:
    """A watch service with its own database connection, like a separate process."""
    notifier = WebhookNotifier(None, default_user_agent(), AddressGuard(allow_private=True))
    return WatchService(
        crawler, notifier, store=WatchStore(path, clock=clock), schedule=False, clock=clock
    )


async def test_each_due_watch_is_checked_once(
    crawler: Crawler, tmp_path: Path, test_site: SiteServer
) -> None:
    clock = Clock()
    database = tmp_path / "watch.sqlite3"
    async with (
        service(crawler, database, clock) as first,
        service(crawler, database, clock) as second,
    ):
        for n in range(3):
            url = test_site.add(f"/claims/{n}", f"<html><body><p>Page {n}</p></body></html>")
            await first.add(WatchCreate.model_validate({"url": url}))

        one, two = await asyncio.gather(first.check_due(), second.check_due())
        checked = [check.watch_id for check in [*one, *two]]
        assert len(checked) == 3 and len(set(checked)) == 3
        assert all(watch.checks == 1 for watch in await first.all())

        clock.now += 3600  # all due again
        one, two = await asyncio.gather(first.check_due(), second.check_due())
        assert len(one) + len(two) == 3
        assert all(watch.checks == 2 for watch in await second.all())


async def test_an_abandoned_claim_runs_out(
    crawler: Crawler, tmp_path: Path, test_site: SiteServer
) -> None:
    clock = Clock()
    database = tmp_path / "watch.sqlite3"
    async with service(crawler, database, clock) as survivor:
        url = test_site.add("/claims/lost", "<html><body><p>Lost</p></body></html>")
        watch = await survivor.add(WatchCreate.model_validate({"url": url}))

        crashed = WatchStore(database, clock=clock)  # claims, then never checks
        assert crashed.claim(watch, CLAIM_LEASE_S)
        crashed.close()

        assert await survivor.check_due() == []
        clock.now += CLAIM_LEASE_S
        assert [check.watch_id for check in await survivor.check_due()] == [watch.id]


async def test_a_claim_fails_when_someone_else_moved_the_watch(tmp_path: Path) -> None:
    clock = Clock()
    store = WatchStore(tmp_path / "watch.sqlite3", clock=clock)
    watch = store.create("https://a.test/", 300, {}, None)
    assert store.claim(watch, 60)
    assert not store.claim(watch, 60)  # the copy read before the first claim is stale
    store.close()
