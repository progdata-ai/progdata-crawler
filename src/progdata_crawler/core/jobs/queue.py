"""Running jobs.

`JobQueue` is the interface; `InProcessQueue` runs jobs as asyncio tasks in the same
process as the `Crawler`, so robots.txt, rate limits and the cache are shared by every
job. A queue backed by Azure Service Bus can implement the same interface later.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.store import JobStore
from progdata_crawler.models.envelope import ErrorCode, ErrorInfo
from progdata_crawler.models.jobs import Job, JobKind, JobStatus
from progdata_crawler.models.page import Page

log = logging.getLogger(__name__)


class JobContext:
    """What a running job uses to report its work."""

    def __init__(self, job: Job, store: JobStore) -> None:
        self.job = job
        self._store = store

    async def set_total(self, total: int | None) -> None:
        await asyncio.to_thread(self._store.set_total, self.job.id, total)

    async def report(
        self, url: str, *, page: Page | None = None, error: ErrorInfo | None = None
    ) -> int:
        """Store the result for one page. Returns its sequence number."""
        return await asyncio.to_thread(self._store.add_result, self.job.id, url, page, error)


JobHandler = Callable[[Job, JobContext], Awaitable[None]]


class JobQueue(Protocol):
    async def submit(self, job_id: str) -> None: ...
    async def cancel(self, job_id: str) -> bool: ...
    async def aclose(self) -> None: ...


class InProcessQueue:
    """Runs jobs as asyncio tasks, at most `max_concurrent` at a time."""

    def __init__(
        self,
        store: JobStore,
        handlers: Mapping[JobKind, JobHandler],
        *,
        max_concurrent: int = 2,
    ) -> None:
        self._store = store
        self._handlers = dict(handlers)
        self._slots = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._closing = False

    async def submit(self, job_id: str) -> None:
        task = asyncio.create_task(self._run(job_id), name=f"job-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def cancel(self, job_id: str) -> bool:
        """Stop a queued or running job. False if it is unknown or already finished."""
        job = await asyncio.to_thread(self._store.get, job_id)
        if job is None or job.status.finished:
            return False
        self._cancelled.add(job_id)
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await asyncio.to_thread(self._store.mark_finished, job_id, JobStatus.CANCELLED)
        return True

    async def wait(self, job_id: str) -> None:
        """Wait until a submitted job has finished running (for tests and short jobs)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop all running jobs. They are marked as interrupted."""
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job_id: str) -> None:
        try:
            async with self._slots:
                job = await asyncio.to_thread(self._store.get, job_id)
                if job is None or job.status is not JobStatus.QUEUED:
                    return
                await asyncio.to_thread(self._store.mark_started, job_id)
                await self._handlers[job.kind](job, JobContext(job, self._store))
        except asyncio.CancelledError:
            status = (
                JobStatus.CANCELLED
                if job_id in self._cancelled and not self._closing
                else JobStatus.INTERRUPTED
            )
            await asyncio.to_thread(self._store.mark_finished, job_id, status)
            return
        except CrawlerError as error:
            await asyncio.to_thread(
                self._store.mark_finished, job_id, JobStatus.FAILED, error.to_error_info()
            )
            return
        except Exception:
            log.exception("job failed", extra={"job_id": job_id})
            info = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error")
            await asyncio.to_thread(self._store.mark_finished, job_id, JobStatus.FAILED, info)
            return
        finally:
            self._cancelled.discard(job_id)
        await asyncio.to_thread(self._store.mark_finished, job_id, JobStatus.SUCCEEDED)
