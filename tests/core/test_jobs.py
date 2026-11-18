import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.cleanup import DAY_S, RetentionCleaner
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.queue import JobContext, JobHandler
from progdata_crawler.core.jobs.store import JobStore
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models import ErrorCode, ErrorInfo, Job, JobKind, JobStatus, Page, Retention
from progdata_crawler.settings import Settings


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


# Handlers used as the BULK kind in these tests.


async def three_pages(job: Job, ctx: JobContext) -> None:
    await ctx.set_total(3)
    for i in range(1, 4):
        if i == 2:
            error = ErrorInfo(code=ErrorCode.FETCH_TIMEOUT, message="slow")
            await ctx.report(f"https://example.com/{i}", error=error)
        else:
            await ctx.report(f"https://example.com/{i}", page=Page(url=f"https://example.com/{i}"))


async def fails_with_crawler_error(job: Job, ctx: JobContext) -> None:
    raise CrawlerError(ErrorCode.ROBOTS_DISALLOWED, "not allowed", url="https://example.com/")


async def crashes(job: Job, ctx: JobContext) -> None:
    raise RuntimeError("bug")


async def runs_forever(job: Job, ctx: JobContext) -> None:
    await ctx.report("https://example.com/first", page=Page(url="https://example.com/first"))
    await asyncio.sleep(3600)


async def many_pages(job: Job, ctx: JobContext) -> None:
    for i in range(250):
        await ctx.report(f"https://example.com/{i}", page=Page(url=f"https://example.com/{i}"))


def manager(
    tmp_path: Path, handler: JobHandler, page_store: PageStore, **settings: object
) -> JobManager:
    config = Settings.model_validate({"data_dir": tmp_path, **settings})
    return JobManager(config, {JobKind.BULK: handler}, page_store=page_store)


@pytest.fixture
async def pages() -> AsyncIterator[PageStore]:
    async with PageStore(":memory:") as store:
        yield store


async def test_job_runs_and_reports_results(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, three_pages, pages) as jobs:
        job = await jobs.submit(JobKind.BULK, {"urls": ["…"]})
        assert job.status is JobStatus.QUEUED
        finished = await jobs.wait(job.id)
        assert finished is not None
        assert finished.status is JobStatus.SUCCEEDED
        assert (finished.progress.total, finished.progress.done, finished.progress.failed) == (
            3,
            2,
            1,
        )
        assert finished.started_at is not None and finished.finished_at is not None
        assert finished.request == {"urls": ["…"]}

        results = await jobs.results(job.id)
        assert [r.seq for r in results] == [1, 2, 3]
        assert results[0].page is not None and results[1].error is not None
        assert results[1].error.code is ErrorCode.FETCH_TIMEOUT


async def test_results_are_paged_with_a_cursor(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, many_pages, pages) as jobs:
        job = await jobs.submit(JobKind.BULK, {})
        await jobs.wait(job.id)
        seen: list[int] = []
        cursor = 0
        while batch := await jobs.results(job.id, after=cursor, limit=100):
            seen.extend(r.seq for r in batch)
            cursor = batch[-1].seq
        assert seen == list(range(1, 251))


async def test_crawler_error_fails_the_job(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, fails_with_crawler_error, pages) as jobs:
        job = await jobs.wait((await jobs.submit(JobKind.BULK, {})).id)
        assert job is not None and job.status is JobStatus.FAILED
        assert job.error is not None and job.error.code is ErrorCode.ROBOTS_DISALLOWED


async def test_unexpected_error_fails_without_leaking(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, crashes, pages) as jobs:
        job = await jobs.wait((await jobs.submit(JobKind.BULK, {})).id)
        assert job is not None and job.status is JobStatus.FAILED
        assert job.error is not None and job.error.code is ErrorCode.INTERNAL_ERROR
        assert "bug" not in job.error.message


async def test_cancel_a_running_job(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, runs_forever, pages) as jobs:
        job = await jobs.submit(JobKind.BULK, {})
        while not await jobs.results(job.id):
            await asyncio.sleep(0.01)
        cancelled = await jobs.cancel(job.id)
        assert cancelled is not None and cancelled.status is JobStatus.CANCELLED
        assert len(await jobs.results(job.id)) == 1  # what it did before is kept
        assert (await jobs.cancel(job.id)) is not None  # cancelling again is harmless


async def test_cancel_a_queued_job(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, runs_forever, pages, max_concurrent_jobs=1) as jobs:
        first = await jobs.submit(JobKind.BULK, {})
        second = await jobs.submit(JobKind.BULK, {})
        cancelled = await jobs.cancel(second.id)
        assert cancelled is not None and cancelled.status is JobStatus.CANCELLED
        assert cancelled.started_at is None
        running = await jobs.get(first.id)
        assert running is not None and not running.status.finished


async def test_unknown_job(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, three_pages, pages) as jobs:
        assert await jobs.get("nope") is None
        assert await jobs.results("nope") == []
        assert await jobs.cancel("nope") is None


async def test_restart_interrupts_running_and_resumes_queued(
    tmp_path: Path, pages: PageStore
) -> None:
    # A previous process left one job running and one queued.
    store = JobStore(tmp_path / "jobs.sqlite3")
    running = store.create(JobKind.BULK, {}, Retention.DEFAULT)
    store.mark_started(running.id)
    queued = store.create(JobKind.BULK, {}, Retention.DEFAULT)
    store.close()

    async with manager(tmp_path, three_pages, pages) as jobs:
        interrupted = await jobs.get(running.id)
        assert interrupted is not None and interrupted.status is JobStatus.INTERRUPTED
        resumed = await jobs.wait(queued.id)
        assert resumed is not None and resumed.status is JobStatus.SUCCEEDED


async def test_closing_marks_running_jobs_interrupted(tmp_path: Path, pages: PageStore) -> None:
    jobs = manager(tmp_path, runs_forever, pages)
    await jobs.start()
    job = await jobs.submit(JobKind.BULK, {})
    while not await jobs.results(job.id):
        await asyncio.sleep(0.01)
    await jobs.aclose()

    store = JobStore(tmp_path / "jobs.sqlite3")
    stopped = store.get(job.id)
    store.close()
    assert stopped is not None and stopped.status is JobStatus.INTERRUPTED


async def test_retain_none_deletes_results_once_read(tmp_path: Path, pages: PageStore) -> None:
    async with manager(tmp_path, three_pages, pages) as jobs:
        job = await jobs.submit(JobKind.BULK, {}, retain=Retention.NONE)
        await jobs.wait(job.id)
        first = await jobs.results(job.id, limit=2)
        assert [r.seq for r in first] == [1, 2]
        rest = await jobs.results(job.id, after=0)
        assert [r.seq for r in rest] == [3]
        assert await jobs.results(job.id) == []


async def test_retention_cleanup_with_fake_clock(tmp_path: Path) -> None:
    clock = Clock()
    jobs = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    async with PageStore(":memory:", clock=clock) as pages:
        old_job = jobs.create(JobKind.BULK, {}, Retention.DEFAULT)
        jobs.add_result(old_job.id, "https://example.com/", Page(url="https://example.com/"))
        jobs.mark_finished(old_job.id, JobStatus.SUCCEEDED)
        old_unfinished = jobs.create(JobKind.BULK, {}, Retention.DEFAULT)
        await pages.put("old", Page(url="https://example.com/old"), rendered=False, body="old")

        clock.now += 8 * DAY_S
        new_job = jobs.create(JobKind.BULK, {}, Retention.DEFAULT)
        jobs.mark_finished(new_job.id, JobStatus.SUCCEEDED)
        await pages.put("new", Page(url="https://example.com/new"), rendered=False, body="new")

        cleaner = RetentionCleaner(jobs, pages, retention_days=7)
        assert await cleaner.run_once() == (1, 1)
        assert jobs.get(old_job.id) is None
        assert jobs.results(old_job.id) == []  # results go with the job
        assert jobs.get(old_unfinished.id) is not None  # never delete unfinished work
        assert jobs.get(new_job.id) is not None
        assert await pages.get("new", max_age_s=DAY_S) is not None
        assert await pages.search("old") == []
    jobs.close()


async def test_cleanup_runs_at_start(tmp_path: Path) -> None:
    clock = Clock()
    store = JobStore(tmp_path / "jobs.sqlite3", clock=clock)
    old = store.create(JobKind.BULK, {}, Retention.DEFAULT)
    store.mark_finished(old.id, JobStatus.SUCCEEDED)
    store._db.execute("UPDATE jobs SET created_at = 0")  # pyright: ignore[reportPrivateUsage]
    store.close()
    async with PageStore(":memory:") as pages, manager(tmp_path, three_pages, pages) as jobs:
        await asyncio.sleep(0.05)
        assert await jobs.get(old.id) is None


def test_retention_settings(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert settings.retention_days == 7
    assert settings.jobs_db_path == tmp_path / "jobs.sqlite3"
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, retention_days=0)
