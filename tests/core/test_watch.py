import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.jobs.notify import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookNotifier,
    verify,
)
from progdata_crawler.core.watch.diff import diff_text
from progdata_crawler.core.watch.scheduler import WatchService
from progdata_crawler.core.watch.store import WatchStore
from progdata_crawler.models import ErrorCode, FetchRequest
from progdata_crawler.models.watch import CheckStatus, WatchCreate
from progdata_crawler.settings import Settings, default_user_agent
from tests.support.site_server import SiteServer

SECRET = "watch-secret"


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def page(text: str) -> str:
    return (
        "<html><head><title>Prices</title></head>"
        f"<body><main><h1>Prices</h1><p>{text}</p></main></body></html>"
    )


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    async with Crawler(Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)) as instance:
        yield instance


def notifier() -> WebhookNotifier:
    return WebhookNotifier(
        SECRET, default_user_agent(), AddressGuard(allow_private=True), retry_delays_s=(0.05,)
    )


@pytest.fixture
async def service(crawler: Crawler, tmp_path: Path) -> AsyncIterator[tuple[WatchService, Clock]]:
    clock = Clock()
    store = WatchStore(tmp_path / "watch.sqlite3", clock=clock)
    async with WatchService(crawler, notifier(), store=store, schedule=False, clock=clock) as svc:
        yield svc, clock


def create(url: str, **fields: Any) -> WatchCreate:
    return WatchCreate.model_validate({"url": url, **fields})


async def test_change_gives_a_diff_and_a_signed_notification(
    service: tuple[WatchService, Clock], test_site: SiteServer
) -> None:
    watches, clock = service
    url = test_site.add("/watched/prices", page("Rye bread costs 50."))
    hook = test_site.url("/hooks/watch")
    watch = await watches.add(create(url, interval_s=600, notify_url=hook))

    first = await watches.check(watch.id)
    assert first.status is CheckStatus.FIRST and first.diff is None

    clock.now += 600
    assert (await watches.check_due())[0].status is CheckStatus.UNCHANGED

    test_site.add("/watched/prices", page("Rye bread costs 55."))
    clock.now += 600
    changed = (await watches.check_due())[0]
    assert changed.status is CheckStatus.CHANGED
    assert (changed.added_lines, changed.removed_lines) == (1, 1)
    assert changed.diff is not None
    assert "-Rye bread costs 50." in changed.diff and "+Rye bread costs 55." in changed.diff

    for _ in range(50):
        if any(r.path == "/hooks/watch" for r in test_site.received):
            break
        await asyncio.sleep(0.05)
    sent = [r for r in test_site.received if r.path == "/hooks/watch"]
    assert len(sent) == 1
    timestamp = int(sent[0].headers[TIMESTAMP_HEADER.lower()])
    assert verify(SECRET, timestamp, sent[0].body, sent[0].headers[SIGNATURE_HEADER.lower()])
    body = json.loads(sent[0].body)
    assert body["event"] == "watch.changed"
    assert body["watch"]["id"] == watch.id
    assert "+Rye bread costs 55." in body["check"]["diff"]

    after = await watches.get(watch.id)
    assert after.last_status is CheckStatus.CHANGED and after.checks == 3
    assert watches.store.versions(watch.id) == 2


async def test_only_due_watches_are_checked(
    service: tuple[WatchService, Clock], test_site: SiteServer
) -> None:
    watches, clock = service
    url = test_site.add("/watched/a", page("A"))
    watch = await watches.add(create(url, interval_s=3600))
    assert len(await watches.check_due()) == 1  # due right away
    assert await watches.check_due() == []
    clock.now += 3599
    assert await watches.check_due() == []
    clock.now += 1
    assert [c.watch_id for c in await watches.check_due()] == [watch.id]


async def test_robots_txt_stops_a_watch(
    service: tuple[WatchService, Clock], test_site: SiteServer
) -> None:
    watches, _ = service
    watch = await watches.add(create(test_site.url("/private/staff.html")))
    check = await watches.check(watch.id)
    assert check.status is CheckStatus.FAILED
    assert check.error is not None and check.error.code is ErrorCode.ROBOTS_DISALLOWED


async def test_removing_deletes_versions(
    service: tuple[WatchService, Clock], test_site: SiteServer
) -> None:
    watches, _ = service
    watch = await watches.add(create(test_site.add("/watched/b", page("B"))))
    await watches.check(watch.id)
    assert watches.store.versions(watch.id) == 1
    await watches.remove(watch.id)
    assert watches.store.versions(watch.id) == 0
    with pytest.raises(CrawlerError) as raised:
        await watches.check(watch.id)
    assert raised.value.code is ErrorCode.NOT_FOUND


async def test_watched_pages_keep_the_index_fresh(
    service: tuple[WatchService, Clock], crawler: Crawler, test_site: SiteServer
) -> None:
    watches, clock = service
    url = test_site.add("/watched/news", page("Old news about spelt."))
    watch = await watches.add(create(url))
    await watches.check(watch.id)
    test_site.add("/watched/news", page("Fresh news about emmer."))
    clock.now += 3600
    await watches.check_due()
    assert [hit.url for hit in await crawler.store.search("emmer")] == [url]


async def test_notify_url_needs_a_secret(
    crawler: Crawler, tmp_path: Path, test_site: SiteServer
) -> None:
    no_secret = WebhookNotifier(None, default_user_agent(), AddressGuard(allow_private=True))
    async with WatchService(crawler, no_secret, schedule=False) as watches:
        with pytest.raises(CrawlerError) as raised:
            await watches.add(create(test_site.url("/"), notify_url=test_site.url("/hooks/x")))
    assert raised.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize(
    "fields",
    [
        {"interval_s": 60},
        {"retain": "none"},
        {"steps": [{"action": "click", "selector": "#x"}]},
    ],
)
def test_invalid_watches(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        create("https://example.test/", **fields)


async def test_the_scheduler_checks_new_watches(crawler: Crawler, test_site: SiteServer) -> None:
    url = test_site.add("/watched/loop", page("Loop"))
    async with WatchService(crawler, notifier()) as watches:
        watch = await watches.add(create(url))
        for _ in range(100):
            if (await watches.get(watch.id)).last_status is not None:
                break
            await asyncio.sleep(0.05)
        assert (await watches.get(watch.id)).last_status is CheckStatus.FIRST


async def test_diff_output_in_fetch(crawler: Crawler, test_site: SiteServer) -> None:
    url = test_site.add("/watched/diff", page("Version one."))
    request = FetchRequest.model_validate({"url": url, "outputs": ["markdown", "diff"]})
    first = (await crawler.fetch(request)).page
    assert first.diff is None  # nothing stored before
    same = (await crawler.fetch(request)).page
    assert same.diff == ""
    test_site.add("/watched/diff", page("Version two."))
    changed = (await crawler.fetch(request)).page
    assert changed.diff is not None and "+Version two." in changed.diff


def test_diff_text() -> None:
    change = diff_text("a\nb\nc", "a\nB\nc\nd")
    assert (change.added_lines, change.removed_lines) == (2, 1)
    assert change.changed
    assert not diff_text("same", "same").changed
