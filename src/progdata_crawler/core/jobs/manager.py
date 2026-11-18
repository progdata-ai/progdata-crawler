"""The job service: create, run, inspect and cancel jobs, and keep the tables tidy."""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from progdata_crawler.core.jobs.cleanup import RetentionCleaner
from progdata_crawler.core.jobs.queue import InProcessQueue, JobHandler
from progdata_crawler.core.jobs.store import JobStore
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models.jobs import Job, JobKind, JobResult, JobStatus
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
    ) -> None:
        self._store = JobStore(store_path if store_path is not None else settings.jobs_db_path)
        self._queue = InProcessQueue(
            self._store, handlers, max_concurrent=settings.max_concurrent_jobs
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
        self._store.close()

    async def submit(
        self, kind: JobKind, request: dict[str, Any], *, retain: Retention = Retention.DEFAULT
    ) -> Job:
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

    async def cleanup(self) -> tuple[int, int]:
        """Run the retention cleanup now."""
        return await self._cleaner.run_once()
