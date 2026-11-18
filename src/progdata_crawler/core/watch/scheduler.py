"""Watches: fetch pages on a schedule, compare with the last version, and notify.

Every check is an ordinary fetch through the `Crawler`, so robots.txt, the per-domain
rate limit and the address guard apply; a page that robots.txt closes simply fails its
checks. A fresh copy is fetched every time (no cache), and the result is stored like any
fetch, which keeps the local full-text index current for the pages you watch.

Pages are compared by their markdown. A version is kept the first time and whenever the
page changes; the change, with a unified diff, is sent to the watch's `notify_url` as a
signed webhook.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.notify import WebhookNotifier
from progdata_crawler.core.watch.diff import diff_text
from progdata_crawler.core.watch.store import WatchStore
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.options import Output
from progdata_crawler.models.watch import CheckStatus, Watch, WatchCheck, WatchCreate

log = logging.getLogger(__name__)

IDLE_SLEEP_S = 60.0
"""Longest the scheduler sleeps between looks at the table (new watches are seen then)."""

CHANGED_EVENT = "watch.changed"


class WatchService:
    """Use as an async context manager. With `schedule=True` a loop checks due watches
    while it is open; without it, call `check_due` yourself (for example from cron)."""

    def __init__(
        self,
        crawler: Crawler,
        notifier: WebhookNotifier,
        *,
        store: WatchStore | None = None,
        schedule: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._crawler = crawler
        self._notifier = notifier
        self._store = store or WatchStore(crawler.settings.watch_db_path, clock=clock)
        self._schedule = schedule
        self._clock = clock
        self._loop: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._checking: set[str] = set()

    @property
    def store(self) -> WatchStore:
        return self._store

    async def __aenter__(self) -> Self:
        if self._schedule:
            self._loop = asyncio.create_task(self._run(), name="watch-scheduler")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
            self._loop = None
        self._store.close()

    async def add(self, request: WatchCreate) -> Watch:
        """Add a watch; it is checked right away. Raises `invalid_request` for a
        `notify_url` without a webhook secret, and `address_not_allowed` for internal
        notify addresses on shared entry points."""
        notify_url = str(request.notify_url) if request.notify_url is not None else None
        if notify_url is not None:
            if not self._notifier.enabled:
                raise CrawlerError(
                    ErrorCode.INVALID_REQUEST,
                    "notify_url needs a webhook secret: set PDC_WEBHOOK_SECRET",
                )
            await self._notifier.check_url(notify_url)
        options = request.model_dump(mode="json", exclude={"url", "interval_s", "notify_url"})
        watch = await asyncio.to_thread(
            self._store.create, str(request.url), request.interval_s, options, notify_url
        )
        log.info("watch added", extra={"watch_id": watch.id})
        self._wake.set()
        return watch

    async def all(self) -> list[Watch]:
        return await asyncio.to_thread(self._store.all)

    async def get(self, watch_id: str) -> Watch:
        watch = await asyncio.to_thread(self._store.get, watch_id)
        if watch is None:
            raise _not_found(watch_id)
        return watch

    async def remove(self, watch_id: str) -> Watch:
        """Remove a watch and all its versions."""
        watch = await self.get(watch_id)
        await asyncio.to_thread(self._store.delete, watch_id)
        log.info("watch removed", extra={"watch_id": watch_id})
        return watch

    async def check(self, watch_id: str) -> WatchCheck:
        """Check one watch now, whether it is due or not."""
        return await self._check(await self.get(watch_id))

    async def check_due(self) -> list[WatchCheck]:
        """Check every watch that is due, one after another."""
        due = await asyncio.to_thread(self._store.due)
        return [await self._check(watch) for watch in due if watch.id not in self._checking]

    async def _check(self, watch: Watch) -> WatchCheck:
        self._checking.add(watch.id)
        try:
            return await self._fetch_and_compare(watch)
        finally:
            self._checking.discard(watch.id)

    async def _fetch_and_compare(self, watch: Watch) -> WatchCheck:
        options: dict[str, Any] = dict(watch.options)
        outputs = list(options.get("outputs") or [])
        if Output.MARKDOWN.value not in outputs:
            outputs.append(Output.MARKDOWN.value)
        request = FetchRequest.model_validate(
            {**options, "url": watch.url, "outputs": outputs, "cache_ttl": 0}
        )
        checked_at = self._clock()
        try:
            page = (await self._crawler.fetch(request)).page
        except CrawlerError as error:
            await asyncio.to_thread(
                self._store.record, watch, CheckStatus.FAILED, markdown=None, at=checked_at
            )
            log.info("watch check failed", extra={"watch_id": watch.id, "code": error.code.value})
            return WatchCheck(
                watch_id=watch.id,
                url=watch.url,
                checked_at=checked_at,
                status=CheckStatus.FAILED,
                error=error.to_error_info(),
            )

        markdown = page.markdown or ""
        previous = await asyncio.to_thread(self._store.latest_version, watch.id)
        if previous is None:
            check = WatchCheck(
                watch_id=watch.id, url=watch.url, checked_at=checked_at, status=CheckStatus.FIRST
            )
        else:
            change = diff_text(previous, markdown, label=watch.url)
            check = WatchCheck(
                watch_id=watch.id,
                url=watch.url,
                checked_at=checked_at,
                status=CheckStatus.CHANGED if change.changed else CheckStatus.UNCHANGED,
                diff=change.text if change.changed else None,
                added_lines=change.added_lines,
                removed_lines=change.removed_lines,
            )
        await asyncio.to_thread(
            self._store.record, watch, check.status, markdown=markdown, at=checked_at
        )
        log.info("watch checked", extra={"watch_id": watch.id, "status": check.status.value})
        if check.status is CheckStatus.CHANGED and watch.notify_url is not None:
            self._notifier.notify_event(
                watch.notify_url,
                CHANGED_EVENT,
                {"watch": watch.model_dump(mode="json"), "check": check.model_dump(mode="json")},
                subject=watch.id,
            )
        return check

    async def _run(self) -> None:
        while True:
            try:
                await self.check_due()
            except Exception:
                log.exception("watch scheduler failed")
            next_due = await asyncio.to_thread(self._store.next_due_at)
            wait_s = IDLE_SLEEP_S
            if next_due is not None:
                wait_s = min(IDLE_SLEEP_S, max(1.0, next_due - self._clock()))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=wait_s)


def _not_found(watch_id: str) -> CrawlerError:
    return CrawlerError(ErrorCode.NOT_FOUND, "no watch with this id", watch_id=watch_id)
