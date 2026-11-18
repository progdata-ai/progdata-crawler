"""MCP server: the core's operations as tools for Claude and other agents.

Start it with `progdata-crawler mcp` (stdio) or `progdata-crawler mcp --http`
(streamable HTTP). Every tool calls the same `Crawler` as the CLI and the REST API.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from progdata_crawler import __version__
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.envelopes import enveloped
from progdata_crawler.core.store.query import query_pages
from progdata_crawler.models import (
    DEFAULT_OUTPUTS,
    ContentMode,
    Device,
    Envelope,
    ErrorCode,
    ErrorInfo,
    FetchRequest,
    Meta,
    Output,
    Page,
    QueryRequest,
    QueryResult,
    RenderMode,
    Retention,
)
from progdata_crawler.settings import Settings

INSTRUCTIONS = (
    "progdata-crawler fetches web pages and returns them as clean markdown and structured data. "
    "It respects robots.txt and rate limits, and does not get around sites that block crawlers. "
    "Errors come back in the `error` field with a stable `code`: robots_disallowed and "
    "blocked_by_site mean the page must not be fetched; rate_limited means try again later."
)

FETCH_DESCRIPTION = (
    "Fetch one web page and return the requested outputs. By default: the main content as "
    "markdown, plus metadata (title, description, language, status). Pages that need "
    "JavaScript are rendered in a browser automatically. Recent results come from a cache."
)


QUERY_DESCRIPTION = (
    "Search the content of pages fetched before (cached), without fetching anything. "
    "Every word must match. Returns URL, title and a snippet with the matches in [brackets], "
    "best matches first. Use pdc_fetch to read a hit in full."
)


def _invalid[T](result_type: type[T], error: ValidationError) -> Envelope[T]:
    first = error.errors()[0]
    return Envelope[result_type](
        meta=Meta(request_id="invalid", took_ms=0),
        error=ErrorInfo(
            code=ErrorCode.INVALID_REQUEST,
            message=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ),
    )


def create_server(settings: Settings, *, store_path: str | None = None) -> MCPServer[None]:
    """Build the MCP server. The crawler starts with the server and stops with it."""
    running: dict[str, Crawler] = {}

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:
        async with Crawler(settings, store_path=store_path) as crawler:
            running["crawler"] = crawler
            try:
                yield None
            finally:
                running.clear()

    server: MCPServer[None] = MCPServer(
        name="progdata-crawler",
        title="progdata-crawler",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool(
        name="pdc_fetch",
        title="Fetch a web page",
        description=FETCH_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def pdc_fetch(  # pyright: ignore[reportUnusedFunction]
        url: Annotated[str, Field(description="The http(s) URL of the page.")],
        outputs: Annotated[
            tuple[Output, ...],
            Field(
                description="What to return: markdown, html (cleaned), raw_html, links, "
                "images, metadata, screenshot (base64 PNG).",
            ),
        ] = DEFAULT_OUTPUTS,
        content: Annotated[
            ContentMode, Field(description="'main' for the main content only, 'full' for all.")
        ] = ContentMode.MAIN,
        include: Annotated[
            list[str] | None, Field(description="CSS selectors of the only elements to keep.")
        ] = None,
        exclude: Annotated[
            list[str] | None, Field(description="CSS selectors of elements to remove.")
        ] = None,
        render: Annotated[
            RenderMode, Field(description="When to use a browser: auto, always or never.")
        ] = RenderMode.AUTO,
        device: Annotated[Device, Field(description="desktop or mobile.")] = Device.DESKTOP,
        locale: Annotated[
            str | None, Field(description="Preferred language, e.g. 'nb-NO'.")
        ] = None,
        wait_ms: Annotated[
            int, Field(description="Extra wait after load in milliseconds (uses a browser).")
        ] = 0,
        timeout_ms: Annotated[
            int | None, Field(description="Time limit for the page in milliseconds.")
        ] = None,
        cache_ttl: Annotated[
            int | None,
            Field(description="Maximum age of a cached result in seconds. 0 fetches fresh."),
        ] = None,
        full_page_screenshot: Annotated[
            bool, Field(description="With the screenshot output: capture the whole page.")
        ] = False,
        retain: Annotated[
            Retention, Field(description="'none' stores nothing after the response.")
        ] = Retention.DEFAULT,
    ) -> Envelope[Page]:
        fields: dict[str, Any] = {
            "url": url,
            "outputs": outputs,
            "content": content,
            "tags": {"include": include or [], "exclude": exclude or []},
            "render": render,
            "device": device,
            "locale": locale,
            "wait_ms": wait_ms,
            "full_page_screenshot": full_page_screenshot,
            "retain": retain,
        }
        if timeout_ms is not None:
            fields["timeout_ms"] = timeout_ms
        if cache_ttl is not None:
            fields["cache_ttl"] = cache_ttl
        try:
            request = FetchRequest.model_validate(fields)
        except ValidationError as error:
            return _invalid(Page, error)
        return await running["crawler"].fetch_envelope(request)

    @server.tool(
        name="pdc_query",
        title="Search fetched pages",
        description=QUERY_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def pdc_query(  # pyright: ignore[reportUnusedFunction]
        query: Annotated[str, Field(description="Words to look for.")],
        limit: Annotated[int, Field(description="Most hits to return (1-100).")] = 10,
    ) -> Envelope[QueryResult]:
        try:
            request = QueryRequest(query=query, limit=limit)
        except ValidationError as error:
            return _invalid(QueryResult, error)
        return await enveloped(QueryResult, query_pages(running["crawler"].store, request))

    return server
