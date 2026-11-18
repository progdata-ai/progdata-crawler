"""The job handlers every entry point runs: harvest and bulk."""

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.harvest import harvest_handler
from progdata_crawler.core.jobs.bulk import bulk_handler
from progdata_crawler.core.jobs.queue import JobHandler
from progdata_crawler.models.jobs import JobKind


def job_handlers(crawler: Crawler) -> dict[JobKind, JobHandler]:
    return {JobKind.HARVEST: harvest_handler(crawler), JobKind.BULK: bulk_handler(crawler)}
