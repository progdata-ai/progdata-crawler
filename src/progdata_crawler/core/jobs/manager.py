"""The job service: create, run, inspect and cancel jobs, and keep the tables tidy."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.jobs.cleanup import RetentionCleaner
from progdata_crawler.core.jobs.notify import WebhookNotifier
from progdata_crawler.core.jobs.queue import InProcessQueue, JobHandler
from progdata_crawler.core.jobs.store import JobStore
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.jobs import Job, JobKind, JobResult, JobStatus, JobView
from progdata_crawler.models.options import Retention
from progdata_crawler.settings import Settings


class JobManager:
    """Use as an async context manager; `start()` runs on enter."""

    def __init__(
        self,
        settings: Settings,
        handlers: Mapping[JobKind, JobHandler],
        *,
        page_store: PageStore,
        store_path: Path | str | None = None,
        notifier: WebhookNotifier | None = None,
    ) -> None:
        self._store = JobStore(store_path if store_path is not None else settings.jobs_db_path)
        self._guard = AddressGuard(allow_private=bool(settings.allow_private_networks))
        secret = settings.webhook_secret
        self._notifier = notifier or WebhookNotifier(
            secret.get_secret_value() if secret is not None else None,
            settings.user_agent,
            self._guard,
        )
        self._queue = InProcessQueue(
            self._store,
            handlers,
            max_concurrent=settings.max_concurrent_jobs,
            on_change=self._status_changed,
        )
        self._cleaner = RetentionCleaner(
            self._store, page_store, retention_days=settings.retention_days
        )

    @property
    def store(self) -> JobStore:
        return self._store

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Mark jobs a previous process left running as interrupted, run the queued ones,
        and start the retention cleanup."""
        await asyncio.to_thread(self._store.interrupt_unfinished)
        for job_id in await asyncio.to_thread(self._store.ids_with_status, JobStatus.QUEUED):
            await self._queue.submit(job_id)
        self._cleaner.start()

    async def aclose(self) -> None:
        await self._cleaner.aclose()
        await self._queue.aclose()
        await self._notifier.aclose()
        self._store.close()

    async def submit(
        self, kind: JobKind, request: dict[str, Any], *, retain: Retention = Retention.DEFAULT
    ) -> Job:
        notify_url = request.get("notify_url")
        if notify_url is not None:
            if not self._notifier.enabled:
                raise CrawlerError(
                    ErrorCode.INVALID_REQUEST,
                    "notify_url needs a webhook secret: set PDC_WEBHOOK_SECRET",
                )
            await self._guard.check_url(str(notify_url))
        job = await asyncio.to_thread(self._store.create, kind, request, retain)
        await self._queue.submit(job.id)
        return job

    async def get(self, job_id: str) -> Job | None:
        return await asyncio.to_thread(self._store.get, job_id)

    async def wait(self, job_id: str) -> Job | None:
        await self._queue.wait(job_id)
        return await self.get(job_id)

    async def results(self, job_id: str, *, after: int = 0, limit: int = 100) -> list[JobResult]:
        """Results after the cursor. With `retain: none`, returned results are deleted."""
        job = await self.get(job_id)
        if job is None:
            return []
        results = await asyncio.to_thread(self._store.results, job_id, after=after, limit=limit)
        if job.retain is Retention.NONE and results:
            await asyncio.to_thread(self._store.delete_results, job_id, up_to=results[-1].seq)
        return results

    async def cancel(self, job_id: str) -> Job | None:
        await self._queue.cancel(job_id)
        return await self.get(job_id)

    async def view(self, job_id: str, *, cursor: int = 0, limit: int = 100) -> JobView:
        """The job's status and its results after `cursor`. Raises `not_found`."""
        job = await self._existing(job_id)
        results = await self.results(job_id, after=cursor, limit=limit)
        next_cursor = results[-1].seq if results else cursor
        return JobView(
            job=job,
            results=tuple(results),
            next_cursor=next_cursor,
            complete=job.status.finished and len(results) < limit,
        )

    async def cancel_job(self, job_id: str) -> Job:
        """Cancel a queued or running job and return it. Finished jobs are left as they are.
        Raises `not_found`."""
        await self._existing(job_id)
        job = await self.cancel(job_id)
        if job is None:  # deleted by the cleanup in the meantime
            raise _not_found(job_id)
        return job

    async def _status_changed(self, job_id: str) -> None:
        job = await self.get(job_id)
        if job is not None:
            self._notifier.notify(job)

    async def _existing(self, job_id: str) -> Job:
        job = await self.get(job_id)
        if job is None:
            raise _not_found(job_id)
        return job

    async def cleanup(self) -> tuple[int, int]:
        """Run the retention cleanup now."""
        return await self._cleaner.run_once()


def _not_found(job_id: str) -> CrawlerError:
    return CrawlerError(ErrorCode.NOT_FOUND, "no job with this id", job_id=job_id)
