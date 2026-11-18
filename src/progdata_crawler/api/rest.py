"""REST API: the core's operations over HTTP, under /v1.

Every response uses the same envelope (`data`, `meta`, `error`). Errors carry a stable
code in `error.code` and the matching HTTP status. Start it with `progdata-crawler serve`.
"""

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from progdata_crawler import __version__
from progdata_crawler.core.convert.documents import (
    MAX_DOCUMENT_BYTES,
    convert_bytes,
    convert_url,
)
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover_envelope
from progdata_crawler.core.envelopes import enveloped
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.find import SearchProvider, create_provider, find
from progdata_crawler.core.jobs.events import job_events
from progdata_crawler.core.jobs.handlers import job_handlers as default_job_handlers
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.queue import JobHandler
from progdata_crawler.core.store.query import query_pages
from progdata_crawler.models import (
    BulkRequest,
    ConvertRequest,
    ConvertResult,
    DiscoverRequest,
    DiscoverResult,
    Envelope,
    ErrorCode,
    ErrorInfo,
    FetchRequest,
    FindRequest,
    FindResult,
    HarvestRequest,
    Job,
    JobEvent,
    JobEventType,
    JobKind,
    JobView,
    Meta,
    Page,
    QueryRequest,
    QueryResult,
)
from progdata_crawler.models.jobs import MAX_JOB_WAIT_S, MAX_RESULTS_PER_REQUEST
from progdata_crawler.settings import Settings

log = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": Envelope[Page], "description": description}
    for status, description in (
        (403, "robots_disallowed: robots.txt does not allow the URL"),
        (415, "unsupported_type: the content type or size cannot be handled"),
        (404, "not_found: no job with this id"),
        (422, "invalid_request: the request failed validation"),
        (429, "rate_limited: the site asked us to slow down"),
        (502, "blocked_by_site or fetch_failed"),
        (504, "fetch_timeout: the page did not respond in time"),
    )
}


def _envelope_response(envelope: Envelope[Any]) -> JSONResponse:
    headers: dict[str, str] = {REQUEST_ID_HEADER: envelope.meta.request_id}
    status = 200
    if envelope.error is not None:
        status = envelope.error.code.http_status
        retry_after = envelope.error.details.get("retry_after_s")
        if isinstance(retry_after, int | float):
            headers["Retry-After"] = str(max(1, round(retry_after)))
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status, headers=headers)


async def _enveloped[T](
    result_type: type[T], operation: Awaitable[T], request: Request
) -> JSONResponse:
    """Run a core operation and wrap its result or `CrawlerError` in an envelope."""
    envelope = await enveloped(
        result_type, operation, request_id=request.headers.get(REQUEST_ID_HEADER)
    )
    return _envelope_response(envelope)


async def _convert_upload(request: Request, name: str) -> ConvertResult:
    """Read the body up to the size limit, then convert it."""
    content = bytearray()
    async for chunk in request.stream():
        content += chunk
        if len(content) > MAX_DOCUMENT_BYTES:
            raise CrawlerError(
                ErrorCode.UNSUPPORTED_TYPE,
                f"the document is larger than {MAX_DOCUMENT_BYTES} bytes",
                max_bytes=MAX_DOCUMENT_BYTES,
            )
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    return await convert_bytes(bytes(content), source=name, content_type=content_type, name=name)


async def _raise(error: CrawlerError) -> Job:
    raise error


def format_event(event: JobEvent) -> str:
    """One event in the Server-Sent Events wire format."""
    if event.type is JobEventType.KEEP_ALIVE:
        return ": keep-alive\n\n"
    payload = event.job if event.type is JobEventType.JOB else event.result
    data = payload.model_dump_json() if payload is not None else "{}"
    return f"id: {event.id}\nevent: {event.type.value}\ndata: {data}\n\n"


async def _sse(events: AsyncIterator[JobEvent], first: JobEvent) -> AsyncIterator[str]:
    yield format_event(first)
    async for event in events:
        yield format_event(event)


def create_app(
    settings: Settings,
    *,
    store_path: str | None = None,
    job_handlers: Mapping[JobKind, JobHandler] | None = None,
    search_provider: SearchProvider | None = None,
) -> FastAPI:
    """Build the REST app. The crawler and the job service start and stop with the app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with (
            Crawler(settings, store_path=store_path) as crawler,
            JobManager(
                settings,
                job_handlers if job_handlers is not None else default_job_handlers(crawler),
                page_store=crawler.store,
            ) as jobs,
        ):
            app.state.crawler = crawler
            app.state.jobs = jobs
            provider = search_provider or create_provider(
                settings, robots=crawler.check_robots, store=crawler.store
            )
            app.state.search = provider
            try:
                yield
            finally:
                await provider.aclose()

    app = FastAPI(
        title="progdata-crawler",
        version=__version__,
        description="Fetch, crawl, search and structure web content for LLMs and agents.",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid request"}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        envelope = Envelope[Page](
            meta=Meta(request_id=request.headers.get(REQUEST_ID_HEADER) or "invalid", took_ms=0),
            error=ErrorInfo(
                code=ErrorCode.INVALID_REQUEST,
                message=f"{location}: {first.get('msg')}" if location else str(first.get("msg")),
                details={"errors": len(exc.errors())},
            ),
        )
        return _envelope_response(envelope)

    @app.post(
        "/v1/fetch",
        response_model=Envelope[Page],
        responses=ERROR_RESPONSES,
        summary="Fetch one page",
        description="Fetch one page and return the requested outputs.",
    )
    async def fetch(  # pyright: ignore[reportUnusedFunction]
        body: FetchRequest, request: Request
    ) -> JSONResponse:
        crawler: Crawler = request.app.state.crawler
        envelope = await crawler.fetch_envelope(
            body, request_id=request.headers.get(REQUEST_ID_HEADER)
        )
        return _envelope_response(envelope)

    @app.post(
        "/v1/discover",
        response_model=Envelope[DiscoverResult],
        responses=ERROR_RESPONSES,
        summary="Discover the URLs of a site",
        description="List a site's URLs from its sitemaps and the links on its start page, "
        "without fetching every page.",
    )
    async def discover(  # pyright: ignore[reportUnusedFunction]
        body: DiscoverRequest, request: Request
    ) -> JSONResponse:
        crawler: Crawler = request.app.state.crawler
        envelope = await discover_envelope(
            crawler, body, request_id=request.headers.get(REQUEST_ID_HEADER)
        )
        return _envelope_response(envelope)

    @app.post(
        "/v1/find",
        response_model=Envelope[FindResult],
        responses={
            **ERROR_RESPONSES,
            503: {"model": Envelope[Page], "description": "search_unavailable"},
        },
        summary="Search the web",
        description="Search the web through the configured provider. With `fetch`, every "
        "web and news hit is also fetched with those page options and returned with it.",
    )
    async def find_endpoint(  # pyright: ignore[reportUnusedFunction]
        body: FindRequest, request: Request
    ) -> JSONResponse:
        crawler: Crawler = request.app.state.crawler
        provider: SearchProvider = request.app.state.search
        return await _enveloped(FindResult, find(crawler, provider, body), request)

    @app.post(
        "/v1/convert",
        response_model=Envelope[ConvertResult],
        responses=ERROR_RESPONSES,
        summary="Convert a document at a URL",
        description="Fetch a PDF, DOCX or XLSX document and convert it to markdown.",
    )
    async def convert(  # pyright: ignore[reportUnusedFunction]
        body: ConvertRequest, request: Request
    ) -> JSONResponse:
        crawler: Crawler = request.app.state.crawler
        return await _enveloped(ConvertResult, convert_url(crawler, str(body.url)), request)

    @app.post(
        "/v1/convert/file",
        response_model=Envelope[ConvertResult],
        responses=ERROR_RESPONSES,
        summary="Convert an uploaded document",
        description="Send a PDF, DOCX or XLSX file as the request body (up to 25 MB) and "
        "get it back as markdown. `name` is the file name, used as the source.",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def convert_file(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        name: Annotated[str, Query(min_length=1, max_length=255)] = "upload",
    ) -> JSONResponse:
        return await _enveloped(ConvertResult, _convert_upload(request, name), request)

    @app.post(
        "/v1/query",
        response_model=Envelope[QueryResult],
        responses=ERROR_RESPONSES,
        summary="Search pages fetched before",
        description="Full-text search in the content of pages already fetched and cached. "
        "Fetches nothing.",
    )
    async def query(  # pyright: ignore[reportUnusedFunction]
        body: QueryRequest, request: Request
    ) -> JSONResponse:
        crawler: Crawler = request.app.state.crawler
        return await _enveloped(QueryResult, query_pages(crawler.store, body), request)

    wait_query = Query(
        ge=0,
        le=MAX_JOB_WAIT_S,
        description="Seconds to wait for the job to finish before answering. With 0 the "
        "job is returned right away; follow it with GET /v1/jobs/{id} or its events.",
    )

    @app.post(
        "/v1/harvest",
        response_model=Envelope[JobView],
        responses=ERROR_RESPONSES,
        summary="Crawl a site (job)",
        description="Start a breadth-first crawl from `url` within the limits, as a job. "
        "Returns the job and, when it finished within `wait_s`, its first results.",
    )
    async def harvest(  # pyright: ignore[reportUnusedFunction]
        body: HarvestRequest,
        request: Request,
        wait_s: Annotated[float, wait_query] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_RESULTS_PER_REQUEST)] = 100,
    ) -> JSONResponse:
        jobs: JobManager = request.app.state.jobs
        operation = jobs.start_job(JobKind.HARVEST, body, wait_s=wait_s, limit=limit)
        return await _enveloped(JobView, operation, request)

    @app.post(
        "/v1/bulk",
        response_model=Envelope[JobView],
        responses=ERROR_RESPONSES,
        summary="Fetch many URLs (job)",
        description="Fetch every URL in `urls` with the same page options, as a job. "
        "Returns the job and, when it finished within `wait_s`, its first results.",
    )
    async def bulk(  # pyright: ignore[reportUnusedFunction]
        body: BulkRequest,
        request: Request,
        wait_s: Annotated[float, wait_query] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_RESULTS_PER_REQUEST)] = 100,
    ) -> JSONResponse:
        jobs: JobManager = request.app.state.jobs
        operation = jobs.start_job(JobKind.BULK, body, wait_s=wait_s, limit=limit)
        return await _enveloped(JobView, operation, request)

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=Envelope[JobView],
        responses=ERROR_RESPONSES,
        summary="Job status and results",
        description="The job's status and progress, and its results after `cursor`. "
        "Read all results by passing `next_cursor` back until `complete` is true.",
    )
    async def get_job(  # pyright: ignore[reportUnusedFunction]
        job_id: str,
        request: Request,
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_RESULTS_PER_REQUEST)] = 100,
    ) -> JSONResponse:
        jobs: JobManager = request.app.state.jobs
        return await _enveloped(JobView, jobs.view(job_id, cursor=cursor, limit=limit), request)

    @app.get(
        "/v1/jobs/{job_id}/events",
        response_class=StreamingResponse,
        responses={
            200: {"content": {"text/event-stream": {}}, "description": "The event stream"},
            404: ERROR_RESPONSES[404],
        },
        summary="Live job events (Server-Sent Events)",
        description="First the job's current state (`job`), then each finished page "
        "(`result`) and every status or progress change (`job`). The stream ends when the "
        "job has finished. Every event id is the `seq` of the last result sent; reconnect "
        "with `Last-Event-ID` to continue after it.",
    )
    async def job_events_stream(  # pyright: ignore[reportUnusedFunction]
        job_id: str,
        request: Request,
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
        cursor: Annotated[int, Query(ge=0)] = 0,
    ) -> Response:
        jobs: JobManager = request.app.state.jobs
        events = aiter(job_events(jobs, job_id, after=last_event_id or cursor))
        try:
            first = await anext(events)  # raises not_found before the stream starts
        except CrawlerError as error:
            return await _enveloped(Job, _raise(error), request)
        return StreamingResponse(
            _sse(events, first),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete(
        "/v1/jobs/{job_id}",
        response_model=Envelope[Job],
        responses=ERROR_RESPONSES,
        summary="Cancel a job",
        description="Stop a queued or running job. A finished job is returned unchanged.",
    )
    async def cancel_job(  # pyright: ignore[reportUnusedFunction]
        job_id: str, request: Request
    ) -> JSONResponse:
        jobs: JobManager = request.app.state.jobs
        return await _enveloped(Job, jobs.cancel_job(job_id), request)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "version": __version__}

    return app
