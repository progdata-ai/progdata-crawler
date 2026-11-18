"""Structured extraction for one URL directly, or many as a job."""

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.models.bulk import BulkRequest
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.jobs import JobKind
from progdata_crawler.models.options import Output
from progdata_crawler.models.structure_request import StructureRequest, StructureResult


async def structure(
    crawler: Crawler,
    jobs: JobManager,
    request: StructureRequest,
    *,
    wait_s: float = 0,
    limit: int = 100,
) -> StructureResult:
    """One URL: fetch it and return the extracted data, or raise the extraction's error.
    Several URLs: start a bulk job with the same options and return it (with its results
    when it finished within `wait_s`)."""
    options = request.model_dump(exclude={"urls"})
    if len(request.urls) == 1:
        fetch_request = FetchRequest.model_validate({**options, "url": request.urls[0]})
        page = (await crawler.fetch(fetch_request)).page
        failure = (page.output_errors or {}).get(Output.STRUCTURED.value)
        if failure is not None:
            raise CrawlerError(failure.code, failure.message, **failure.details)
        return StructureResult(url=page.url, data=page.structured)
    bulk = BulkRequest.model_validate({**options, "urls": request.urls})
    view = await jobs.start_job(JobKind.BULK, bulk, wait_s=wait_s, limit=limit)
    return StructureResult(job=view)
