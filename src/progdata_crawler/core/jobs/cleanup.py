"""Retention cleanup: delete old cached pages and finished jobs on a schedule."""

import asyncio
import contextlib
import logging

from progdata_crawler.core.jobs.store import JobStore
from progdata_crawler.core.store.sqlite import PageStore

log = logging.getLogger(__name__)

DAY_S = 24 * 60 * 60
DEFAULT_INTERVAL_S = 60 * 60


class RetentionCleaner:
    """Deletes data older than `retention_days`: at start, then every `interval_s`."""

    def __init__(
        self,
        jobs: JobStore,
        pages: PageStore,
        *,
        retention_days: int,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._jobs = jobs
        self._pages = pages
        self._max_age_s = retention_days * DAY_S
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> tuple[int, int]:
        """Delete old data now. Returns (jobs deleted, cached pages deleted)."""
        jobs = await asyncio.to_thread(self._jobs.delete_older_than, self._max_age_s)
        pages = await self._pages.delete_older_than(self._max_age_s)
        if jobs or pages:
            log.info("retention cleanup", extra={"jobs_deleted": jobs, "pages_deleted": pages})
        return jobs, pages

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="retention-cleanup")

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("retention cleanup failed")
            await asyncio.sleep(self._interval_s)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
