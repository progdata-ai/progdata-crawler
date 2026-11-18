"""Harvest: a breadth-first crawl from a start page, within limits.

Every page goes through the `Crawler`, so the address guard, robots.txt, the per-domain
rate limit and the cache apply. Pages are reported to the job as they finish, so progress
and partial results are visible while the crawl runs.
"""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover
from progdata_crawler.core.discover.discover import in_scope, matches_paths
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.harvest.goal import MAX_LINKS_PER_CALL, rank_links
from progdata_crawler.core.jobs.queue import JobContext, JobHandler
from progdata_crawler.core.structure.llm import Llm
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


@dataclass
class _Candidate:
    url: str
    depth: int
    text: str
    order: int
    score: float | None = None
    """Relevance to the goal from the LLM, 0 to 1; None when not ranked."""


class _Frontier:
    """URLs still to fetch, without duplicates. Without scores they come out breadth
    first; with scores, the most relevant first."""

    def __init__(self, request: HarvestRequest) -> None:
        self._request = request
        self._start = str(request.url)
        self._seen = UrlDeduplicator()
        self._counter = 0
        self.pending: list[_Candidate] = []

    def add(self, links: Iterable[tuple[str, str]], depth: int) -> None:
        if depth > self._request.max_depth:
            return
        for url, text in links:
            if not in_scope(url, self._start, self._request.scope):
                continue
            if not matches_paths(url, self._request.paths):
                continue
            if self._seen.add(url):
                self._counter += 1
                self.pending.append(_Candidate(url, depth, text, self._counter))

    def mark_seen(self, url: str) -> None:
        self._seen.add(url)

    def unranked(self, limit: int) -> list[_Candidate]:
        waiting = [c for c in self.pending if c.score is None]
        return sorted(waiting, key=lambda c: (c.depth, c.order))[:limit]

    def take(self, count: int) -> list[_Candidate]:
        """The next `count` candidates: ranked ones by score, then the rest breadth first.
        While some are relevant to the goal, only those are taken, so the links they
        lead to can be ranked before anything less relevant is fetched."""
        self.pending.sort(
            key=lambda c: (-(c.score if c.score is not None else -1.0), c.depth, c.order)
        )
        relevant = sum(1 for c in self.pending if c.score is not None and c.score > 0)
        if relevant:
            count = min(count, relevant)
        batch, self.pending = self.pending[:count], self.pending[count:]
        return batch


_REQUEST_ONLY = {
    "url",
    "max_pages",
    "max_depth",
    "paths",
    "scope",
    "sitemaps",
    "outputs",
    "notify_url",
    "goal",
    "goal_budget",
}


async def harvest(
    crawler: Crawler, request: HarvestRequest, ctx: JobContext, *, llm: Llm | None = None
) -> None:
    """Crawl and report each page to `ctx`. Raises `CrawlerError` when the start page
    cannot be fetched, which fails the job. With a goal, `llm` (default: the crawler's)
    ranks the links."""
    wants_links = Output.LINKS in request.outputs
    outputs = request.outputs if wants_links else (*request.outputs, Output.LINKS)
    options = request.model_dump(exclude=_REQUEST_ONLY)
    frontier = _Frontier(request)
    reported = 0
    llm_calls_left = request.goal_budget if request.goal else 0

    def page_request(url: str) -> FetchRequest:
        return FetchRequest.model_validate({**options, "url": url, "outputs": outputs})

    async def report_page(url: str, page: Page) -> list[tuple[str, str]]:
        nonlocal reported
        reported += 1
        frontier.mark_seen(page.url)  # the final URL after redirects
        links = [(link.url, link.text) for link in page.links or () if "nofollow" not in link.rel]
        if not wants_links:
            page = page.model_copy(update={"links": None})
        await ctx.report(url, page=page)
        return links

    async def fetch_one(candidate: _Candidate) -> None:
        """Fetch and report one page, and queue its links."""
        nonlocal reported
        if reported >= request.max_pages:
            return
        try:
            outcome = await crawler.fetch(page_request(candidate.url))
        except CrawlerError as error:
            if error.code in _SKIPPED or reported >= request.max_pages:
                return
            reported += 1
            await ctx.report(candidate.url, error=error.to_error_info())
            return
        if reported >= request.max_pages:
            return
        links = await report_page(candidate.url, outcome.page)
        frontier.add(links, candidate.depth + 1)

    async def rank() -> None:
        nonlocal llm_calls_left
        batch = frontier.unranked(MAX_LINKS_PER_CALL)
        if not batch or request.goal is None:
            return
        try:
            model = llm or crawler.llm()
            scores = await rank_links(model, request.goal, [(c.url, c.text) for c in batch])
        except CrawlerError as error:
            log.warning(
                "goal ranking failed, going on breadth first",
                extra={"job_id": ctx.job.id, "code": error.code.value},
            )
            llm_calls_left = 0
            return
        llm_calls_left -= 1
        for candidate, score in zip(batch, scores, strict=True):
            candidate.score = score

    start = str(request.url)
    frontier.mark_seen(start)
    await ctx.set_total(1)
    start_page = (await crawler.fetch(page_request(start))).page
    start_links = await report_page(start, start_page)
    frontier.add(start_links, 1)
    if request.sitemaps and request.max_depth >= 1:
        frontier.add(((url, "") for url in await _sitemap_urls(crawler, request)), 1)

    while reported < request.max_pages and frontier.pending:
        if llm_calls_left > 0:
            await rank()
        batch = frontier.take(min(CONCURRENCY, request.max_pages - reported))
        await ctx.set_total(min(request.max_pages, reported + len(batch) + len(frontier.pending)))
        await asyncio.gather(*(fetch_one(candidate) for candidate in batch))
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
