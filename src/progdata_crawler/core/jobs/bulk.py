"""Bulk: fetch many URLs in one job.

Pages are fetched in parallel through the `Crawler`, so the address guard, robots.txt,
the shared per-domain rate limit and the cache apply. A semaphore shared by all bulk jobs
caps how many pages are fetched at once. Every URL gets a result or an error; one failing
URL does not fail the job.
"""

import asyncio
import logging

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.queue import JobContext, JobHandler
from progdata_crawler.core.urls import UrlDeduplicator
from progdata_crawler.models.bulk import BulkRequest
from progdata_crawler.models.envelope import ErrorCode, ErrorInfo
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.jobs import Job

log = logging.getLogger(__name__)


def unique_urls(request: BulkRequest) -> list[str]:
    seen = UrlDeduplicator()
    return [str(url) for url in request.urls if seen.add(str(url))]


async def bulk(
    crawler: Crawler, request: BulkRequest, ctx: JobContext, slots: asyncio.Semaphore
) -> None:
    """Fetch every URL in `request` and report each one to `ctx`."""
    options = request.model_dump(exclude={"urls", "notify_url"})
    urls = unique_urls(request)
    await ctx.set_total(len(urls))

    async def fetch_one(url: str) -> None:
        async with slots:
            try:
                outcome = await crawler.fetch(FetchRequest.model_validate({**options, "url": url}))
            except CrawlerError as error:
                await ctx.report(url, error=error.to_error_info())
                return
            except Exception:
                log.exception("unexpected error", extra={"job_id": ctx.job.id, "url": url})
                info = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error")
                await ctx.report(url, error=info)
                return
        await ctx.report(url, page=outcome.page)

    await asyncio.gather(*(fetch_one(url) for url in urls))
    log.info("bulk finished", extra={"job_id": ctx.job.id, "urls": len(urls)})


def bulk_handler(crawler: Crawler) -> JobHandler:
    """The job handler for `JobKind.BULK`. All jobs run by it share one concurrency limit."""
    slots = asyncio.Semaphore(crawler.settings.bulk_concurrency)

    async def run(job: Job, ctx: JobContext) -> None:
        await bulk(crawler, BulkRequest.model_validate(job.request), ctx, slots)

    return run
