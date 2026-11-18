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
from progdata_crawler.core.convert.documents import convert_url
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover_envelope
from progdata_crawler.core.envelopes import enveloped
from progdata_crawler.core.find import SearchProvider, create_provider, find
from progdata_crawler.core.jobs.handlers import job_handlers
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.store.query import query_pages
from progdata_crawler.models import (
    DEFAULT_OUTPUTS,
    BulkRequest,
    ContentMode,
    ConvertResult,
    Device,
    DiscoverRequest,
    DiscoverResult,
    Envelope,
    ErrorCode,
    ErrorInfo,
    FetchRequest,
    FindRequest,
    FindResult,
    HarvestRequest,
    JobKind,
    JobView,
    Meta,
    Output,
    Page,
    QueryRequest,
    QueryResult,
    RenderMode,
    Retention,
    Scope,
    SearchSource,
    TimeRange,
)
from progdata_crawler.models.jobs import MAX_JOB_WAIT_S
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


DISCOVER_DESCRIPTION = (
    "List the URLs of a site from its sitemaps and the links on its start page, without "
    "fetching every page. Filter with `query` (words in the URL or link text), path "
    "regexes and `scope`. Use it to find the pages worth fetching."
)

HARVEST_DESCRIPTION = (
    "Crawl a site breadth first from `url`, within `max_pages` and `max_depth`, and return "
    "each page with the requested outputs. Runs as a job: waits up to `wait_s` seconds and "
    "returns the results when the job finished in time; otherwise returns the running job. "
    "Then call pdc_job with its id and `next_cursor` until `complete` is true."
)

BULK_DESCRIPTION = (
    "Fetch many URLs with the same options, in parallel. Runs as a job: waits up to "
    "`wait_s` seconds and returns the results when the job finished in time; otherwise "
    "returns the running job. Then call pdc_job with its id and `next_cursor`."
)

JOB_DESCRIPTION = (
    "Status, progress and results of a harvest or bulk job, `limit` results after "
    "`cursor`. Pass `next_cursor` back until `complete` is true. With cancel=true the job "
    "is stopped first."
)

WaitS = Annotated[
    float,
    Field(
        ge=0,
        le=MAX_JOB_WAIT_S,
        description="Seconds to wait for the job to finish before answering.",
    ),
]
Outputs = Annotated[
    tuple[Output, ...],
    Field(description="What to return per page: markdown, html, raw_html, links, metadata …"),
]
Content = Annotated[
    ContentMode, Field(description="'main' for the main content only, 'full' for all.")
]
Render = Annotated[RenderMode, Field(description="When to use a browser: auto, always, never.")]
Retain = Annotated[Retention, Field(description="'none' stores nothing after the job is read.")]
IncludePaths = Annotated[
    list[str] | None, Field(description="Regexes; only URL paths matching one are kept.")
]
ExcludePaths = Annotated[
    list[str] | None, Field(description="Regexes; URL paths matching one are left out.")
]
ScopeArg = Annotated[
    Scope, Field(description="'site' (same host), 'subdomains' or 'external' (any host).")
]


def _invalid[T](result_type: type[T], error: ValidationError) -> Envelope[T]:
    first = error.errors()[0]
    return Envelope[result_type](
        meta=Meta(request_id="invalid", took_ms=0),
        error=ErrorInfo(
            code=ErrorCode.INVALID_REQUEST,
            message=f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}",
        ),
    )


FIND_DESCRIPTION = (
    "Search the web. Returns hits with URL, title and snippet. With fetch=true every web and "
    "news hit is also fetched and returned with the requested outputs (markdown by "
    "default), so one call gives search results you can read."
)

CONVERT_DESCRIPTION = (
    "Fetch a PDF, DOCX or XLSX document from a URL and return it as markdown. (pdc_fetch "
    "also converts documents; this tool returns only the document's markdown and title.)"
)


def create_server(
    settings: Settings,
    *,
    store_path: str | None = None,
    search_provider: SearchProvider | None = None,
) -> MCPServer[None]:
    """Build the MCP server. The crawler starts with the server and stops with it."""
    running: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:
        async with (
            Crawler(settings, store_path=store_path) as crawler,
            JobManager(settings, job_handlers(crawler), page_store=crawler.store) as jobs,
        ):
            provider = search_provider or create_provider(settings, crawler.check_robots)
            running["crawler"] = crawler
            running["jobs"] = jobs
            running["search"] = provider
            try:
                yield None
            finally:
                running.clear()
                await provider.aclose()

    def crawler() -> Crawler:
        return running["crawler"]

    def jobs() -> JobManager:
        return running["jobs"]

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
        return await crawler().fetch_envelope(request)

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
        return await enveloped(QueryResult, query_pages(crawler().store, request))

    @server.tool(
        name="pdc_find",
        title="Search the web",
        description=FIND_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def pdc_find(  # pyright: ignore[reportUnusedFunction]
        query: Annotated[str, Field(description="What to search for.")],
        sources: Annotated[
            tuple[SearchSource, ...], Field(description="web, news and/or images.")
        ] = (SearchSource.WEB,),
        time_range: Annotated[
            TimeRange | None, Field(description="Only results from the last day/week/month/year.")
        ] = None,
        limit: Annotated[int, Field(description="Most results per source (1-20).")] = 10,
        country: Annotated[str | None, Field(description="Country code, e.g. 'NO'.")] = None,
        language: Annotated[str | None, Field(description="Language code, e.g. 'nb'.")] = None,
        fetch: Annotated[bool, Field(description="Also fetch every web and news hit.")] = False,
        outputs: Outputs = (Output.MARKDOWN,),
    ) -> Envelope[FindResult]:
        try:
            request = FindRequest.model_validate(
                {
                    "query": query,
                    "sources": sources,
                    "time_range": time_range,
                    "limit": limit,
                    "country": country,
                    "language": language,
                    "fetch": {"outputs": outputs} if fetch else None,
                }
            )
        except ValidationError as error:
            return _invalid(FindResult, error)
        return await enveloped(FindResult, find(crawler(), running["search"], request))

    @server.tool(
        name="pdc_convert",
        title="Convert a document",
        description=CONVERT_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def pdc_convert(  # pyright: ignore[reportUnusedFunction]
        url: Annotated[str, Field(description="The http(s) URL of the document.")],
    ) -> Envelope[ConvertResult]:
        if not url.startswith(("http://", "https://")):
            return Envelope[ConvertResult](
                meta=Meta(request_id="invalid", took_ms=0),
                error=ErrorInfo(
                    code=ErrorCode.INVALID_REQUEST, message="url: must be an http(s) URL"
                ),
            )
        return await enveloped(ConvertResult, convert_url(crawler(), url))

    @server.tool(
        name="pdc_discover",
        title="List the URLs of a site",
        description=DISCOVER_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def pdc_discover(  # pyright: ignore[reportUnusedFunction]
        url: Annotated[str, Field(description="The start page of the site.")],
        query: Annotated[
            str | None, Field(description="Keep URLs whose address or link text has every word.")
        ] = None,
        include_paths: IncludePaths = None,
        exclude_paths: ExcludePaths = None,
        scope: ScopeArg = Scope.SITE,
        sitemaps: Annotated[bool, Field(description="Read the site's sitemaps.")] = True,
        links: Annotated[bool, Field(description="Read the links on the start page.")] = True,
        limit: Annotated[int, Field(description="Most URLs to return.")] = 1_000,
    ) -> Envelope[DiscoverResult]:
        try:
            request = DiscoverRequest.model_validate(
                {
                    "url": url,
                    "query": query,
                    "paths": {"include": include_paths or [], "exclude": exclude_paths or []},
                    "scope": scope,
                    "sitemaps": sitemaps,
                    "links": links,
                    "limit": limit,
                }
            )
        except ValidationError as error:
            return _invalid(DiscoverResult, error)
        return await discover_envelope(crawler(), request)

    @server.tool(
        name="pdc_harvest",
        title="Crawl a site",
        description=HARVEST_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def pdc_harvest(  # pyright: ignore[reportUnusedFunction]
        url: Annotated[str, Field(description="The start page.")],
        max_pages: Annotated[int, Field(description="Most pages to fetch.")] = 20,
        max_depth: Annotated[int, Field(description="Most links to follow from the start.")] = 2,
        include_paths: IncludePaths = None,
        exclude_paths: ExcludePaths = None,
        scope: ScopeArg = Scope.SITE,
        sitemaps: Annotated[bool, Field(description="Also start from the sitemaps.")] = True,
        outputs: Outputs = DEFAULT_OUTPUTS,
        content: Content = ContentMode.MAIN,
        render: Render = RenderMode.AUTO,
        retain: Retain = Retention.DEFAULT,
        wait_s: WaitS = 30,
        limit: Annotated[int, Field(description="Most results in this answer.")] = 100,
    ) -> Envelope[JobView]:
        try:
            request = HarvestRequest.model_validate(
                {
                    "url": url,
                    "max_pages": max_pages,
                    "max_depth": max_depth,
                    "paths": {"include": include_paths or [], "exclude": exclude_paths or []},
                    "scope": scope,
                    "sitemaps": sitemaps,
                    "outputs": outputs,
                    "content": content,
                    "render": render,
                    "retain": retain,
                }
            )
        except ValidationError as error:
            return _invalid(JobView, error)
        operation = jobs().start_job(JobKind.HARVEST, request, wait_s=wait_s, limit=limit)
        return await enveloped(JobView, operation)

    @server.tool(
        name="pdc_bulk",
        title="Fetch many pages",
        description=BULK_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def pdc_bulk(  # pyright: ignore[reportUnusedFunction]
        urls: Annotated[list[str], Field(description="The http(s) URLs to fetch.")],
        outputs: Outputs = DEFAULT_OUTPUTS,
        content: Content = ContentMode.MAIN,
        render: Render = RenderMode.AUTO,
        retain: Retain = Retention.DEFAULT,
        wait_s: WaitS = 30,
        limit: Annotated[int, Field(description="Most results in this answer.")] = 100,
    ) -> Envelope[JobView]:
        try:
            request = BulkRequest.model_validate(
                {
                    "urls": urls,
                    "outputs": outputs,
                    "content": content,
                    "render": render,
                    "retain": retain,
                }
            )
        except ValidationError as error:
            return _invalid(JobView, error)
        operation = jobs().start_job(JobKind.BULK, request, wait_s=wait_s, limit=limit)
        return await enveloped(JobView, operation)

    @server.tool(
        name="pdc_job",
        title="Follow a job",
        description=JOB_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    async def pdc_job(  # pyright: ignore[reportUnusedFunction]
        job_id: Annotated[str, Field(description="The job id from pdc_harvest or pdc_bulk.")],
        cursor: Annotated[int, Field(ge=0, description="Results after this seq.")] = 0,
        limit: Annotated[int, Field(ge=1, le=1_000, description="Most results.")] = 100,
        cancel: Annotated[bool, Field(description="Stop the job first.")] = False,
    ) -> Envelope[JobView]:
        async def view() -> JobView:
            if cancel:
                await jobs().cancel_job(job_id)
            return await jobs().view(job_id, cursor=cursor, limit=limit)

        return await enveloped(JobView, view())

    return server
