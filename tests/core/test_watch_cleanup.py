import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.jobs.cleanup import DAY_S, RetentionCleaner
from progdata_crawler.core.jobs.notify import WebhookNotifier
from progdata_crawler.core.jobs.store import JobStore
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.core.watch.scheduler import WatchService
from progdata_crawler.core.watch.store import WatchStore
from progdata_crawler.models.watch import CheckStatus, WatchCreate
from progdata_crawler.settings import Settings, default_user_agent
from tests.support.site_server import SiteServer


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def version_times(store: WatchStore, watch_id: str) -> list[float]:
    with store._lock:  # pyright: ignore[reportPrivateUsage]
        rows = store._db.execute(  # pyright: ignore[reportPrivateUsage]
            "SELECT checked_at FROM watch_versions WHERE watch_id = ? ORDER BY checked_at",
            (watch_id,),
        ).fetchall()
    return [row[0] for row in rows]


def test_old_versions_go_but_the_newest_stays(tmp_path: Path) -> None:
    clock = Clock()
    store = WatchStore(tmp_path / "watch.sqlite3", clock=clock)
    busy = store.create("https://a.test/", 300, {}, None)
    quiet = store.create("https://b.test/", 300, {}, None)
    start = clock.now
    for day in (0, 2, 9):  # busy changes three times
        store.record(busy, CheckStatus.CHANGED, markdown=f"v{day}", at=start + day * DAY_S)
    store.record(quiet, CheckStatus.FIRST, markdown="only", at=start)  # quiet never changes

    clock.now = start + 10 * DAY_S
    deleted = store.delete_versions_older_than(7 * DAY_S)

    assert deleted == 2  # busy's day-0 and day-2 versions
    assert version_times(store, busy.id) == [start + 9 * DAY_S]
    assert version_times(store, quiet.id) == [start]  # old, but the newest it has
    assert store.latest_version(quiet.id) == "only"
    store.close()


def test_removing_a_watch_removes_every_version(tmp_path: Path) -> None:
    clock = Clock()
    store = WatchStore(tmp_path / "watch.sqlite3", clock=clock)
    watch = store.create("https://a.test/", 300, {}, None)
    for n in range(3):
        store.record(watch, CheckStatus.CHANGED, markdown=f"v{n}", at=clock.now + n)
    store.delete(watch.id)
    assert version_times(store, watch.id) == []
    store.close()


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1, retention_days=7)
    async with Crawler(settings) as instance:
        yield instance


async def test_service_cleanup_is_logged(
    crawler: Crawler, tmp_path: Path, test_site: SiteServer, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    store = WatchStore(tmp_path / "w.sqlite3", clock=clock)
    notifier = WebhookNotifier(None, default_user_agent(), AddressGuard(allow_private=True))
    async with WatchService(crawler, notifier, store=store, schedule=False, clock=clock) as watches:
        url = test_site.add("/cleanup/page", "<html><body><main><p>One</p></main></body></html>")
        watch = await watches.add(WatchCreate.model_validate({"url": url, "interval_s": 300}))
        await watches.check(watch.id)
        test_site.add("/cleanup/page", "<html><body><main><p>Two</p></main></body></html>")
        clock.now += 8 * DAY_S
        await watches.check(watch.id)
        clock.now += 1 * DAY_S
        with caplog.at_level(logging.INFO):
            deleted = await watches.cleanup()
    assert deleted == 1
    record = next(r for r in caplog.records if r.getMessage() == "watch retention cleanup")
    assert record.__dict__["versions_deleted"] == 1
    assert "One" not in repr(record.__dict__) and "Two" not in repr(record.__dict__)


async def test_job_and_cache_cleanup_logs_every_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    jobs = JobStore(tmp_path / "jobs.sqlite3")
    async with PageStore(tmp_path / "pages.sqlite3") as pages:
        cleaner = RetentionCleaner(jobs, pages, retention_days=7)
        with caplog.at_level(logging.INFO):
            assert await cleaner.run_once() == (0, 0)
    jobs.close()
    record = next(r for r in caplog.records if r.getMessage() == "retention cleanup")
    assert (record.__dict__["jobs_deleted"], record.__dict__["pages_deleted"]) == (0, 0)
