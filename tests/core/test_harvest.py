from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.harvest import harvest_handler
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.models import ErrorCode, Job, JobKind, JobResult, JobStatus
from progdata_crawler.settings import Settings
from tests.support.site_server import PUBLIC_PAGES, SiteServer

ROBOTS = "User-agent: *\nDisallow: /private/\n"


@pytest.fixture
async def jobs(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[JobManager]:
    test_site.add("/robots.txt", ROBOTS, content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(
            settings, {JobKind.HARVEST: harvest_handler(crawler)}, page_store=crawler.store
        ) as manager,
    ):
        yield manager


async def run(jobs: JobManager, **request: Any) -> tuple[Job, list[JobResult]]:
    job = await jobs.submit(JobKind.HARVEST, request)
    finished = await jobs.wait(job.id)
    assert finished is not None
    return finished, await jobs.results(job.id, limit=1000)


def page(html_body: str) -> str:
    return f"<html><head><title>Test</title></head><body>{html_body}</body></html>"


async def test_crawls_the_site_once_without_disallowed_pages(
    jobs: JobManager, test_site: SiteServer
) -> None:
    job, results = await run(jobs, url=test_site.url("/"))

    assert job.status is JobStatus.SUCCEEDED
    urls = [result.url for result in results]
    assert len(urls) == len(set(urls))
    assert set(urls) == {test_site.url(path) for path in PUBLIC_PAGES}
    assert all(result.page is not None and result.error is None for result in results)
    assert all("/private/" not in url for url in urls)
    assert results[0].url == test_site.url("/")
    assert job.progress.total == job.progress.done == len(PUBLIC_PAGES)
    # Links were used to crawl but not asked for.
    assert all(result.page is not None and result.page.links is None for result in results)


async def test_max_pages(jobs: JobManager, test_site: SiteServer) -> None:
    job, results = await run(jobs, url=test_site.url("/"), max_pages=3)
    assert len(results) == 3
    assert job.progress.done == 3


async def test_max_depth(jobs: JobManager, test_site: SiteServer) -> None:
    for i in range(1, 5):
        test_site.add(f"/chain/{i}", page(f'<p>Step {i}</p><a href="/chain/{i + 1}">next</a>'))
    start = test_site.url("/chain/1")

    _, results = await run(jobs, url=start, max_depth=2, sitemaps=False)
    assert [result.url for result in results] == [test_site.url(f"/chain/{i}") for i in (1, 2, 3)]

    _, only_start = await run(jobs, url=start, max_depth=0)
    assert [result.url for result in only_start] == [start]


async def test_paths_and_sitemap_seeds(jobs: JobManager, test_site: SiteServer) -> None:
    _, results = await run(jobs, url=test_site.url("/"), paths={"include": ["^/docs/"]})
    urls = {result.url for result in results}
    assert urls == {test_site.url("/")} | {
        test_site.url(path) for path in PUBLIC_PAGES if path.startswith("/docs/")
    }


async def test_requested_outputs_apply_to_every_page(
    jobs: JobManager, test_site: SiteServer
) -> None:
    _, results = await run(
        jobs, url=test_site.url("/docs/"), outputs=["links", "metadata"], max_depth=1
    )
    assert len(results) > 1
    for result in results:
        assert result.page is not None
        assert result.page.links is not None
        assert result.page.markdown is None


async def test_failing_pages_are_reported_and_the_job_goes_on(
    jobs: JobManager, test_site: SiteServer
) -> None:
    start = test_site.add(
        "/broken-links",
        page('<a href="/status/403">blocked</a> <a href="/broken-ok">fine</a>'),
    )
    test_site.add("/broken-ok", page("<p>Fine.</p>"))
    job, results = await run(jobs, url=start, sitemaps=False)

    assert job.status is JobStatus.SUCCEEDED
    by_url = {result.url: result for result in results}
    blocked = by_url[test_site.url("/status/403")]
    assert blocked.error is not None and blocked.error.code is ErrorCode.BLOCKED_BY_SITE
    assert by_url[test_site.url("/broken-ok")].page is not None
    assert (job.progress.done, job.progress.failed) == (2, 1)


async def test_failing_start_page_fails_the_job(jobs: JobManager, test_site: SiteServer) -> None:
    job, results = await run(jobs, url=test_site.url("/private/staff.html"))
    assert job.status is JobStatus.FAILED
    assert job.error is not None and job.error.code is ErrorCode.ROBOTS_DISALLOWED
    assert results == []


async def test_nofollow_links_are_not_followed(jobs: JobManager, test_site: SiteServer) -> None:
    start = test_site.add(
        "/nofollow", page('<a href="/followed">a</a> <a rel="nofollow" href="/skipped">b</a>')
    )
    test_site.add("/followed", page("<p>A</p>"))
    test_site.add("/skipped", page("<p>B</p>"))
    _, results = await run(jobs, url=start, sitemaps=False)
    assert [result.url for result in results] == [start, test_site.url("/followed")]
