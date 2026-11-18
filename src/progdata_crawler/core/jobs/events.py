"""Live job events: status and progress changes, and each finished page.

Events are built from the job table, so they work no matter which worker runs the job.
Every event carries the sequence number of the last result sent as its id. A client
that reconnects with that id gets the current job state again and then the results
after it, without gaps or duplicates.
"""

import asyncio
from collections.abc import AsyncIterator

from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.models.jobs import Job, JobEvent, JobEventType

POLL_S = 0.25
"""How often the job table is checked for news."""

KEEP_ALIVE_S = 15.0
"""A keep-alive event is sent when nothing else happened for this long."""

BATCH = 100


async def job_events(
    jobs: JobManager,
    job_id: str,
    *,
    after: int = 0,
    poll_s: float = POLL_S,
    keep_alive_s: float = KEEP_ALIVE_S,
) -> AsyncIterator[JobEvent]:
    """Yield the job's state, then results after `after` and changes as they happen.
    Ends after the job has finished and every result has been sent. Raises `not_found`
    when the job does not exist."""
    job = await jobs.view_job(job_id)
    cursor = after
    yield JobEvent(id=cursor, type=JobEventType.JOB, job=job)
    last_sent = asyncio.get_running_loop().time()
    previous: Job = job

    while True:
        # Read the state before the results, so a job seen as finished has no results left
        # after this batch.
        current = await jobs.get(job_id)
        if current is None:  # deleted by the retention cleanup
            return
        results = await jobs.results(job_id, after=cursor, limit=BATCH)
        for result in results:
            cursor = result.seq
            yield JobEvent(id=cursor, type=JobEventType.RESULT, result=result)
        sent = bool(results)
        if _changed(previous, current):
            yield JobEvent(id=cursor, type=JobEventType.JOB, job=current)
            previous, sent = current, True
        if sent:
            last_sent = asyncio.get_running_loop().time()
        if current.status.finished and len(results) < BATCH:
            return
        if results:
            continue  # more may be waiting
        if asyncio.get_running_loop().time() - last_sent >= keep_alive_s:
            yield JobEvent(id=cursor, type=JobEventType.KEEP_ALIVE)
            last_sent = asyncio.get_running_loop().time()
        await asyncio.sleep(poll_s)


def _changed(before: Job, after: Job) -> bool:
    return (before.status, before.progress) != (after.status, after.progress)
