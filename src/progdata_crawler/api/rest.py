"""REST API: the core's operations over HTTP, under /v1.

Every response uses the same envelope (`data`, `meta`, `error`). Errors carry a stable
code in `error.code` and the matching HTTP status. Start it with `progdata-crawler serve`.
"""

import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from progdata_crawler import __version__
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover_envelope
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.jobs.events import job_events
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.queue import JobHandler
from progdata_crawler.models import (
    DiscoverRequest,
    DiscoverResult,
    Envelope,
    ErrorCode,
    ErrorInfo,
    FetchRequest,
    Job,
    JobEvent,
    JobEventType,
    JobKind,
    JobView,
    Meta,
    Page,
)
from progdata_crawler.models.jobs import MAX_RESULTS_PER_REQUEST
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
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    started = time.monotonic()
    data: T | None = None
    error: ErrorInfo | None = None
    try:
        data = await operation
    except CrawlerError as exc:
        error = exc.to_error_info()
    except Exception:
        log.exception("unexpected error", extra={"request_id": request_id})
        error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error")
    meta = Meta(request_id=request_id, took_ms=round((time.monotonic() - started) * 1000))
    return _envelope_response(Envelope[result_type](data=data, meta=meta, error=error))


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
) -> FastAPI:
    """Build the REST app. The crawler and the job service start and stop with the app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with (
            Crawler(settings, store_path=store_path) as crawler,
            JobManager(settings, job_handlers or {}, page_store=crawler.store) as jobs,
        ):
            app.state.crawler = crawler
            app.state.jobs = jobs
            yield

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
