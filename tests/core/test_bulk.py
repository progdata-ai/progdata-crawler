import asyncio
import sys
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.jobs.bulk import bulk_handler
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.models import BulkRequest, ErrorCode, Job, JobKind, JobResult, JobStatus
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer, run_site

ROBOTS = "User-agent: *\nDisallow: /private/\n"


@pytest.fixture
async def jobs(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[JobManager]:
    test_site.add("/robots.txt", ROBOTS, content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1, bulk_concurrency=8)
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(settings, {JobKind.BULK: bulk_handler(crawler)}, page_store=crawler.store) as m,
    ):
        yield m


async def run(jobs: JobManager, **request: Any) -> tuple[Job, list[JobResult]]:
    job = await jobs.submit(JobKind.BULK, request)
    finished = await jobs.wait(job.id)
    assert finished is not None
    return finished, await jobs.results(job.id, limit=1000)


@pytest.fixture
def more_sites() -> Iterator[list[SiteServer]]:
    """Seven more test sites, each on its own loopback address, so they count as
    different domains for the rate limiter."""
    if sys.platform != "linux":
        pytest.skip("binding to 127.0.0.2 and up needs Linux")
    with ExitStack() as stack:
        sites = [stack.enter_context(run_site(f"127.0.0.{i}")) for i in range(2, 9)]
        for site in sites:
            site.add("/robots.txt", ROBOTS, content_type="text/plain")
        yield sites


def page(text: str) -> str:
    return f"<html><head><title>{text}</title></head><body><p>{text}</p></body></html>"


async def test_200_urls_across_several_domains(
    jobs: JobManager, test_site: SiteServer, more_sites: list[SiteServer]
) -> None:
    sites = [test_site, *more_sites]
    urls: list[str] = []
    for n in range(200):
        site = sites[n % len(sites)]
        if n % 50 == 7:
            urls.append(site.url("/private/staff.html"))  # robots.txt says no
        elif n % 50 == 13:
            urls.append(site.url("/status/403"))  # the site blocks us
        else:
            urls.append(site.add(f"/bulk/{n}", page(f"Page {n}")))

    loop = asyncio.get_running_loop()
    started = loop.time()
    job, results = await run(jobs, urls=urls)
    took = loop.time() - started

    assert job.status is JobStatus.SUCCEEDED
    assert sorted(result.url for result in results) == sorted(urls)
    errors = Counter(result.error.code for result in results if result.error is not None)
    assert errors == {ErrorCode.ROBOTS_DISALLOWED: 4, ErrorCode.BLOCKED_BY_SITE: 4}
    assert job.progress.total == 200
    assert (job.progress.done, job.progress.failed) == (192, 8)
    for result in results:
        if result.page is not None:
            assert result.page.metadata is not None
            n = result.url.rsplit("/", 1)[1]
            assert result.page.metadata.title == f"Page {n}"
    # 25 URLs per domain at 0.1 s each: in parallel across domains, not one after another.
    assert took < 15


async def test_duplicates_are_fetched_once(jobs: JobManager, test_site: SiteServer) -> None:
    url = test_site.add("/bulk/dup", page("Dup"))
    _, results = await run(jobs, urls=[url, url, url + "#section"])
    assert [result.url for result in results] == [url]


async def test_page_options_apply_to_every_url(jobs: JobManager, test_site: SiteServer) -> None:
    urls = [test_site.add(f"/bulk/o{i}", page(f"O{i}")) for i in range(3)]
    _, results = await run(jobs, urls=urls, outputs=["links"])
    assert len(results) == 3
    for result in results:
        assert result.page is not None
        assert result.page.links is not None and result.page.markdown is None


async def test_concurrency_is_shared_by_all_bulk_jobs(
    tmp_path: Path, test_site: SiteServer
) -> None:
    test_site.add("/robots.txt", ROBOTS, content_type="text/plain")
    settings = Settings(data_dir=tmp_path, bulk_concurrency=1, max_concurrent_jobs=4)
    async with Crawler(settings, store_path=":memory:") as crawler:
        handler = bulk_handler(crawler)
        active = peak = 0
        original = crawler.fetch

        async def counting_fetch(*args: Any, **kwargs: Any) -> Any:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                return await original(*args, **kwargs)
            finally:
                active -= 1

        crawler.fetch = counting_fetch  # type: ignore[method-assign]
        async with JobManager(settings, {JobKind.BULK: handler}, page_store=crawler.store) as m:
            ids = [
                (await m.submit(JobKind.BULK, {"urls": [test_site.url(f"/slow?ms=50&j={j}")]})).id
                for j in range(3)
            ]
            for job_id in ids:
                await m.wait(job_id)
        assert peak == 1


def test_request_limits() -> None:
    with pytest.raises(ValueError):
        BulkRequest.model_validate({"urls": []})
