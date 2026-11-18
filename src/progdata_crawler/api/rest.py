"""REST API: the core's operations over HTTP, under /v1.

Every response uses the same envelope (`data`, `meta`, `error`). Errors carry a stable
code in `error.code` and the matching HTTP status. Start it with `progdata-crawler serve`.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from progdata_crawler import __version__
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.models import Envelope, ErrorCode, ErrorInfo, FetchRequest, Meta, Page
from progdata_crawler.settings import Settings

REQUEST_ID_HEADER = "X-Request-ID"

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": Envelope[Page], "description": description}
    for status, description in (
        (403, "robots_disallowed: robots.txt does not allow the URL"),
        (415, "unsupported_type: the content type or size cannot be handled"),
        (422, "invalid_request: the request failed validation"),
        (429, "rate_limited: the site asked us to slow down"),
        (502, "blocked_by_site or fetch_failed"),
        (504, "fetch_timeout: the page did not respond in time"),
    )
}


def _envelope_response(envelope: Envelope[Page]) -> JSONResponse:
    headers: dict[str, str] = {REQUEST_ID_HEADER: envelope.meta.request_id}
    status = 200
    if envelope.error is not None:
        status = envelope.error.code.http_status
        retry_after = envelope.error.details.get("retry_after_s")
        if isinstance(retry_after, int | float):
            headers["Retry-After"] = str(max(1, round(retry_after)))
    return JSONResponse(envelope.model_dump(mode="json"), status_code=status, headers=headers)


def create_app(settings: Settings, *, store_path: str | None = None) -> FastAPI:
    """Build the REST app. The crawler starts and stops with the app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with Crawler(settings, store_path=store_path) as crawler:
            app.state.crawler = crawler
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

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "version": __version__}

    return app
