"""Harvest: a breadth-first crawl from a start page, within limits.

Every page goes through the `Crawler`, so the address guard, robots.txt, the per-domain
rate limit and the cache apply. Pages are reported to the job as they finish, so progress
and partial results are visible while the crawl runs.
"""

import asyncio
import logging
from collections.abc import Iterable

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover
from progdata_crawler.core.discover.discover import in_scope, matches_paths
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.queue import JobContext, JobHandler
from progdata_crawler.core.urls import UrlDeduplicator
from progdata_crawler.models.discover import DiscoverRequest
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.harvest import HarvestRequest
from progdata_crawler.models.jobs import Job
from progdata_crawler.models.options import Output
from progdata_crawler.models.page import Page

log = logging.getLogger(__name__)

CONCURRENCY = 4
"""Pages fetched at the same time within one harvest. The per-domain rate limit still
spaces out requests to the same site."""

_SKIPPED = (ErrorCode.ROBOTS_DISALLOWED, ErrorCode.ADDRESS_NOT_ALLOWED)
"""Pages we may not fetch are left out instead of being reported as errors."""


class _Frontier:
    """URLs still to fetch, one depth at a time, without duplicates."""

    def __init__(self, request: HarvestRequest) -> None:
        self._request = request
        self._start = str(request.url)
        self._seen = UrlDeduplicator()
        self.next_level: list[str] = []

    def add(self, urls: Iterable[str]) -> None:
        for url in urls:
            if not in_scope(url, self._start, self._request.scope):
                continue
            if not matches_paths(url, self._request.paths):
                continue
            if self._seen.add(url):
                self.next_level.append(url)

    def mark_seen(self, url: str) -> None:
        self._seen.add(url)

    def take_level(self) -> list[str]:
        level, self.next_level = self.next_level, []
        return level


_REQUEST_ONLY = {
    "url",
    "max_pages",
    "max_depth",
    "paths",
    "scope",
    "sitemaps",
    "outputs",
    "notify_url",
}


async def harvest(crawler: Crawler, request: HarvestRequest, ctx: JobContext) -> None:
    """Crawl and report each page to `ctx`. Raises `CrawlerError` when the start page
    cannot be fetched, which fails the job."""
    wants_links = Output.LINKS in request.outputs
    outputs = request.outputs if wants_links else (*request.outputs, Output.LINKS)
    options = request.model_dump(exclude=_REQUEST_ONLY)
    frontier = _Frontier(request)
    slots = asyncio.Semaphore(CONCURRENCY)
    reported = 0

    def page_request(url: str) -> FetchRequest:
        return FetchRequest.model_validate({**options, "url": url, "outputs": outputs})

    async def report_page(url: str, page: Page) -> list[str]:
        nonlocal reported
        reported += 1
        frontier.mark_seen(page.url)  # the final URL after redirects
        links = [link.url for link in page.links or () if "nofollow" not in link.rel]
        if not wants_links:
            page = page.model_copy(update={"links": None})
        await ctx.report(url, page=page)
        return links

    async def fetch_one(url: str) -> list[str]:
        """Fetch and report one page. Returns its links."""
        nonlocal reported
        async with slots:
            if reported >= request.max_pages:
                return []
            try:
                outcome = await crawler.fetch(page_request(url))
            except CrawlerError as error:
                if error.code in _SKIPPED or reported >= request.max_pages:
                    return []
                reported += 1
                await ctx.report(url, error=error.to_error_info())
                return []
            if reported >= request.max_pages:
                return []
            return await report_page(url, outcome.page)

    start = str(request.url)
    frontier.mark_seen(start)
    await ctx.set_total(1)
    start_page = (await crawler.fetch(page_request(start))).page
    start_links = await report_page(start, start_page)
    if request.max_depth >= 1:
        frontier.add(start_links)
        if request.sitemaps:
            frontier.add(await _sitemap_urls(crawler, request))

    depth = 1
    while depth <= request.max_depth and reported < request.max_pages:
        level = frontier.take_level()
        if not level:
            break
        await ctx.set_total(min(request.max_pages, reported + len(level)))
        found = await asyncio.gather(*(fetch_one(url) for url in level))
        if depth < request.max_depth:
            for links in found:
                frontier.add(links)
        depth += 1
    await ctx.set_total(reported)
    log.info("harvest finished", extra={"job_id": ctx.job.id, "pages": reported})


async def _sitemap_urls(crawler: Crawler, request: HarvestRequest) -> list[str]:
    discovered = await discover(
        crawler,
        DiscoverRequest(
            url=request.url,
            paths=request.paths,
            scope=request.scope,
            links=False,
            limit=request.max_pages,
        ),
    )
    return [item.url for item in discovered.urls]


def harvest_handler(crawler: Crawler) -> JobHandler:
    """The job handler for `JobKind.HARVEST`."""

    async def run(job: Job, ctx: JobContext) -> None:
        await harvest(crawler, HarvestRequest.model_validate(job.request), ctx)

    return run
