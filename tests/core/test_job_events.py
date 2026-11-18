import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.events import job_events
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.queue import JobContext
from progdata_crawler.models import ErrorCode, Job, JobEvent, JobEventType, JobKind, JobStatus, Page
from progdata_crawler.settings import Settings

POLL = 0.02


async def five_slow_pages(job: Job, ctx: JobContext) -> None:
    await ctx.set_total(5)
    for i in range(1, 6):
        await asyncio.sleep(0.05)
        await ctx.report(f"https://example.test/{i}", page=Page(url=f"https://example.test/{i}"))


async def quiet_then_one_page(job: Job, ctx: JobContext) -> None:
    await asyncio.sleep(0.3)
    await ctx.report("https://example.test/late", page=Page(url="https://example.test/late"))


@pytest.fixture
async def jobs(tmp_path: Path) -> AsyncIterator[JobManager]:
    settings = Settings(data_dir=tmp_path)
    handlers = {JobKind.BULK: five_slow_pages, JobKind.HARVEST: quiet_then_one_page}
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(settings, handlers, page_store=crawler.store) as manager,
    ):
        yield manager


async def collect(events: AsyncIterator[JobEvent]) -> list[JobEvent]:
    return [event async for event in events]


def result_seqs(events: list[JobEvent]) -> list[int]:
    return [event.result.seq for event in events if event.result is not None]


async def test_live_events_in_order(jobs: JobManager) -> None:
    job = await jobs.submit(JobKind.BULK, {})
    events = await collect(job_events(jobs, job.id, poll_s=POLL))

    assert events[0].type is JobEventType.JOB
    assert events[0].job is not None and not events[0].job.status.finished
    assert result_seqs(events) == [1, 2, 3, 4, 5]
    assert [event.result.url for event in events if event.result is not None] == [
        f"https://example.test/{i}" for i in range(1, 6)
    ]
    ids = [event.id for event in events]
    assert ids == sorted(ids)
    last = events[-1]
    assert last.type is JobEventType.JOB and last.job is not None
    assert last.job.status is JobStatus.SUCCEEDED
    assert last.job.progress.done == 5
    # Status changes were sent while the job ran, not only at the end.
    statuses = [event.job.status for event in events if event.job is not None]
    assert JobStatus.RUNNING in statuses


async def test_late_client_gets_the_state_then_everything(jobs: JobManager) -> None:
    job = await jobs.submit(JobKind.BULK, {})
    await jobs.wait(job.id)
    events = await collect(job_events(jobs, job.id, poll_s=POLL))
    assert events[0].job is not None and events[0].job.status is JobStatus.SUCCEEDED
    assert result_seqs(events) == [1, 2, 3, 4, 5]
    assert events[-1].type is JobEventType.RESULT  # nothing changed after connecting


async def test_resume_after_a_dropped_connection(jobs: JobManager) -> None:
    job = await jobs.submit(JobKind.BULK, {})
    first: list[JobEvent] = []
    async for event in job_events(jobs, job.id, poll_s=POLL):
        first.append(event)
        if event.result is not None and event.result.seq == 2:
            break  # the connection drops
    resumed = await collect(job_events(jobs, job.id, after=first[-1].id, poll_s=POLL))
    assert result_seqs(first) + result_seqs(resumed) == [1, 2, 3, 4, 5]
    assert resumed[0].type is JobEventType.JOB


async def test_keep_alive_while_nothing_happens(jobs: JobManager) -> None:
    job = await jobs.submit(JobKind.HARVEST, {})
    events = await collect(job_events(jobs, job.id, poll_s=0.02, keep_alive_s=0.1))
    types = [event.type for event in events]
    assert JobEventType.KEEP_ALIVE in types
    assert types.index(JobEventType.KEEP_ALIVE) < types.index(JobEventType.RESULT)


async def test_unknown_job(jobs: JobManager) -> None:
    with pytest.raises(CrawlerError) as raised:
        await collect(job_events(jobs, "nope"))
    assert raised.value.code is ErrorCode.NOT_FOUND
