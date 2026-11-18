"""Command-line interface: `progdata-crawler`, also installed as `pdc`."""

import asyncio
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from progdata_crawler import __version__
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover_envelope
from progdata_crawler.core.envelopes import enveloped
from progdata_crawler.core.jobs.events import job_events
from progdata_crawler.core.jobs.handlers import job_handlers
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.store.query import query_pages
from progdata_crawler.log import configure_logging
from progdata_crawler.models import (
    BulkRequest,
    ContentMode,
    Device,
    DiscoverRequest,
    DiscoverResult,
    Envelope,
    FetchRequest,
    HarvestRequest,
    Job,
    JobKind,
    JobResult,
    JobStatus,
    JobView,
    Output,
    Page,
    QueryRequest,
    QueryResult,
    RenderMode,
    Retention,
    Scope,
)
from progdata_crawler.settings import Settings, get_settings

app = typer.Typer(
    name="progdata-crawler",
    help="Fetch, crawl, search and structure web content for LLMs and agents.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"progdata-crawler {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Fetch, crawl, search and structure web content for LLMs and agents."""


@app.command()
def fetch(
    url: Annotated[str, typer.Argument(help="The page to fetch.")],
    output: Annotated[
        list[Output] | None,
        typer.Option(
            "--output",
            "-o",
            help="Output to return. Repeat for several. Default: markdown and metadata.",
        ),
    ] = None,
    content: Annotated[
        ContentMode, typer.Option(help="Only the main content, or the whole page.")
    ] = ContentMode.MAIN,
    include: Annotated[
        list[str] | None, typer.Option(help="CSS selector of elements to keep. Repeatable.")
    ] = None,
    exclude: Annotated[
        list[str] | None, typer.Option(help="CSS selector of elements to remove. Repeatable.")
    ] = None,
    render: Annotated[RenderMode, typer.Option(help="When to use a browser.")] = RenderMode.AUTO,
    device: Annotated[Device, typer.Option(help="Viewport and User-Agent variant.")] = (
        Device.DESKTOP
    ),
    locale: Annotated[str | None, typer.Option(help="Preferred language, e.g. nb-NO.")] = None,
    wait_ms: Annotated[int, typer.Option(help="Extra wait after load, in milliseconds.")] = 0,
    timeout_ms: Annotated[int | None, typer.Option(help="Time limit for the page.")] = None,
    cache_ttl: Annotated[
        int | None, typer.Option(help="Maximum age of a cached result in seconds. 0 = fresh.")
    ] = None,
    full_page: Annotated[
        bool, typer.Option("--full-page", help="Screenshot the whole page.")
    ] = False,
    no_store: Annotated[
        bool, typer.Option("--no-store", help="Store nothing after the response (retain=none).")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """Fetch one page and print it as markdown, or as JSON with --json."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    fields: dict[str, Any] = {
        "url": url,
        "content": content,
        "tags": {"include": include or [], "exclude": exclude or []},
        "render": render,
        "device": device,
        "locale": locale,
        "wait_ms": wait_ms,
        "full_page_screenshot": full_page,
        "retain": Retention.NONE if no_store else Retention.DEFAULT,
    }
    if output:
        fields["outputs"] = output
    if timeout_ms is not None:
        fields["timeout_ms"] = timeout_ms
    if cache_ttl is not None:
        fields["cache_ttl"] = cache_ttl
    try:
        request = FetchRequest.model_validate(fields)
    except ValidationError as error:
        typer.echo(f"Invalid options: {error.errors()[0]['msg']}", err=True)
        raise typer.Exit(2) from error

    envelope = asyncio.run(_fetch(_local(settings), request))

    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        typer.echo(_human_readable(envelope.data))
    if envelope.error is not None:
        raise typer.Exit(1)


async def _fetch(settings: Settings, request: FetchRequest) -> Envelope[Page]:
    async with Crawler(settings) as crawler:
        return await crawler.fetch_envelope(request)


def _local(settings: Settings) -> Settings:
    """Entry points that run on the user's own machine may fetch internal addresses."""
    if settings.allow_private_networks is None:
        return settings.model_copy(update={"allow_private_networks": True})
    return settings


def _shared(settings: Settings) -> Settings:
    """Entry points that others can reach refuse internal addresses unless told otherwise."""
    if settings.allow_private_networks is None:
        return settings.model_copy(update={"allow_private_networks": False})
    return settings


def _human_readable(page: Page) -> str:
    """Markdown when we have it; otherwise the other outputs as JSON."""
    if page.markdown is not None:
        return page.markdown
    return page.model_dump_json(indent=2, exclude_none=True)


@app.command()
def discover(
    url: Annotated[str, typer.Argument(help="The start page of the site.")],
    query: Annotated[
        str | None, typer.Option(help="Keep URLs whose address or link text has every word.")
    ] = None,
    include_path: Annotated[
        list[str] | None, typer.Option(help="Regex a URL path must match. Repeatable.")
    ] = None,
    exclude_path: Annotated[
        list[str] | None, typer.Option(help="Regex of URL paths to leave out. Repeatable.")
    ] = None,
    scope: Annotated[Scope, typer.Option(help="How far from the start host to go.")] = Scope.SITE,
    sitemaps: Annotated[
        bool, typer.Option("--sitemaps/--no-sitemaps", help="Read the sitemaps.")
    ] = True,
    links: Annotated[
        bool, typer.Option("--links/--no-links", help="Read the links on the start page.")
    ] = True,
    limit: Annotated[int, typer.Option(help="Most URLs to return.")] = 1_000,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """List the URLs of a site from its sitemaps and start page, one per line."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        request = DiscoverRequest.model_validate(
            {
                "url": url,
                "query": query,
                "paths": {"include": include_path or [], "exclude": exclude_path or []},
                "scope": scope,
                "sitemaps": sitemaps,
                "links": links,
                "limit": limit,
            }
        )
    except ValidationError as error:
        typer.echo(f"Invalid options: {error.errors()[0]['msg']}", err=True)
        raise typer.Exit(2) from error

    envelope = asyncio.run(_discover(_local(settings), request))

    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        for item in envelope.data.urls:
            typer.echo(item.url)
        if envelope.data.truncated:
            typer.echo(f"(stopped at --limit {limit})", err=True)
    if envelope.error is not None:
        raise typer.Exit(1)


async def _discover(settings: Settings, request: DiscoverRequest) -> Envelope[DiscoverResult]:
    async with Crawler(settings) as crawler:
        return await discover_envelope(crawler, request)


OutputOption = Annotated[
    list[Output] | None,
    typer.Option(
        "--output", "-o", help="Output per page. Repeatable. Default: markdown, metadata."
    ),
]
JsonLinesOption = Annotated[
    bool, typer.Option("--json", help="Print each result as one line of JSON.")
]


def _page_options(
    output: list[Output] | None, content: ContentMode, render: RenderMode, no_store: bool
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "content": content,
        "render": render,
        "retain": Retention.NONE if no_store else Retention.DEFAULT,
    }
    if output:
        fields["outputs"] = output
    return fields


def _print_result(result: JobResult, *, as_json: bool) -> None:
    if as_json:
        typer.echo(result.model_dump_json(exclude_none=True))
    elif result.error is not None:
        typer.echo(f"✗ {result.url}  {result.error.code.value}: {result.error.message}")
    else:
        title = result.page.metadata.title if result.page and result.page.metadata else None
        typer.echo(f"✓ {result.url}" + (f"  {title}" if title else ""))


async def _run_job(
    settings: Settings, kind: JobKind, request: HarvestRequest | BulkRequest, *, as_json: bool
) -> Job:
    """Run a job in this process, print each result as it finishes, and return the end
    state. Interrupting the command interrupts the job."""
    async with (
        Crawler(settings) as crawler,
        JobManager(settings, job_handlers(crawler), page_store=crawler.store) as jobs,
    ):
        submitted = await jobs.submit(kind, request.model_dump(mode="json"), retain=request.retain)
        typer.echo(f"Job {submitted.id}", err=True)
        async for event in job_events(jobs, submitted.id):
            if event.result is not None:
                _print_result(event.result, as_json=as_json)
        return await jobs.view_job(submitted.id)


def _finish(job: Job) -> None:
    summary = f"{job.status.value}: {job.progress.done} pages, {job.progress.failed} failed"
    typer.echo(summary, err=True)
    if job.error is not None:
        typer.echo(f"Error ({job.error.code.value}): {job.error.message}", err=True)
    if job.status is not JobStatus.SUCCEEDED:
        raise typer.Exit(1)


def _invalid_options(error: ValidationError) -> typer.Exit:
    first = error.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    typer.echo(f"Invalid options: {location}: {first['msg']}", err=True)
    return typer.Exit(2)


@app.command()
def harvest(
    url: Annotated[str, typer.Argument(help="The start page.")],
    max_pages: Annotated[int, typer.Option(help="Most pages to fetch.")] = 100,
    max_depth: Annotated[int, typer.Option(help="Most links to follow from the start.")] = 3,
    include_path: Annotated[
        list[str] | None, typer.Option(help="Regex a URL path must match. Repeatable.")
    ] = None,
    exclude_path: Annotated[
        list[str] | None, typer.Option(help="Regex of URL paths to leave out. Repeatable.")
    ] = None,
    scope: Annotated[Scope, typer.Option(help="How far from the start host to go.")] = Scope.SITE,
    sitemaps: Annotated[
        bool, typer.Option("--sitemaps/--no-sitemaps", help="Also start from the sitemaps.")
    ] = True,
    output: OutputOption = None,
    content: Annotated[ContentMode, typer.Option(help="Main content or whole page.")] = (
        ContentMode.MAIN
    ),
    render: Annotated[RenderMode, typer.Option(help="When to use a browser.")] = RenderMode.AUTO,
    no_store: Annotated[
        bool, typer.Option("--no-store", help="Store nothing once read (retain=none).")
    ] = False,
    as_json: JsonLinesOption = False,
) -> None:
    """Crawl a site breadth first, printing each page as it finishes."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        request = HarvestRequest.model_validate(
            {
                "url": url,
                "max_pages": max_pages,
                "max_depth": max_depth,
                "paths": {"include": include_path or [], "exclude": exclude_path or []},
                "scope": scope,
                "sitemaps": sitemaps,
                **_page_options(output, content, render, no_store),
            }
        )
    except ValidationError as error:
        raise _invalid_options(error) from error
    _finish(asyncio.run(_run_job(_local(settings), JobKind.HARVEST, request, as_json=as_json)))


@app.command()
def bulk(
    urls: Annotated[list[str] | None, typer.Argument(help="URLs to fetch.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File with one URL per line; - for stdin.")
    ] = None,
    output: OutputOption = None,
    content: Annotated[ContentMode, typer.Option(help="Main content or whole page.")] = (
        ContentMode.MAIN
    ),
    render: Annotated[RenderMode, typer.Option(help="When to use a browser.")] = RenderMode.AUTO,
    no_store: Annotated[
        bool, typer.Option("--no-store", help="Store nothing once read (retain=none).")
    ] = False,
    as_json: JsonLinesOption = False,
) -> None:
    """Fetch many URLs in parallel, printing each page as it finishes."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    all_urls = list(urls or [])
    if file is not None:
        text = sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")
        all_urls += [line.strip() for line in text.splitlines() if line.strip()]
    try:
        request = BulkRequest.model_validate(
            {"urls": all_urls, **_page_options(output, content, render, no_store)}
        )
    except ValidationError as error:
        raise _invalid_options(error) from error
    _finish(asyncio.run(_run_job(_local(settings), JobKind.BULK, request, as_json=as_json)))


@app.command()
def job(
    job_id: Annotated[str, typer.Argument(help="The job id.")],
    cursor: Annotated[int, typer.Option(help="Show results after this seq.")] = 0,
    limit: Annotated[int, typer.Option(help="Most results to show.")] = 100,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """Show a job from this machine's job table: status, progress and results."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    envelope = asyncio.run(_job_view(settings, job_id, cursor=cursor, limit=limit))
    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        view = envelope.data
        progress = view.job.progress
        typer.echo(
            f"{view.job.kind.value} {view.job.id}: {view.job.status.value}, "
            f"{progress.done} pages, {progress.failed} failed",
            err=True,
        )
        for result in view.results:
            _print_result(result, as_json=False)
        if not view.complete:
            typer.echo(f"More: --cursor {view.next_cursor}", err=True)
    if envelope.error is not None:
        raise typer.Exit(1)


async def _job_view(
    settings: Settings, job_id: str, *, cursor: int, limit: int
) -> Envelope[JobView]:
    """Read the job table without running jobs: jobs left unfinished by another process
    must not be marked as interrupted by merely looking at them."""
    async with Crawler(settings) as crawler:
        jobs = JobManager(settings, {}, page_store=crawler.store)
        try:
            return await enveloped(JobView, jobs.view(job_id, cursor=cursor, limit=limit))
        finally:
            await jobs.aclose()


@app.command(name="query")
def query_command(
    words: Annotated[list[str], typer.Argument(help="Words to look for. All must match.")],
    limit: Annotated[int, typer.Option(help="Most hits to return.")] = 10,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """Search the content of pages fetched before. Fetches nothing."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        request = QueryRequest(query=" ".join(words), limit=limit)
    except ValidationError as error:
        typer.echo(f"Invalid options: {error.errors()[0]['msg']}", err=True)
        raise typer.Exit(2) from error

    envelope = asyncio.run(_query(settings, request))

    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        if not envelope.data.hits:
            typer.echo("No matches.", err=True)
        for hit in envelope.data.hits:
            typer.echo(f"{hit.title or hit.url}\n  {hit.url}\n  {hit.snippet}\n")
    if envelope.error is not None:
        raise typer.Exit(1)


async def _query(settings: Settings, request: QueryRequest) -> Envelope[QueryResult]:
    async with Crawler(settings) as crawler:
        return await enveloped(QueryResult, query_pages(crawler.store, request))


@app.command()
def mcp(
    http: Annotated[
        bool, typer.Option("--http", help="Serve streamable HTTP instead of stdio.")
    ] = False,
    host: Annotated[str, typer.Option(help="Interface for --http.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for --http.")] = 8765,
) -> None:
    """Run the MCP server, over stdio by default (for Claude Desktop and Claude Code)."""
    from progdata_crawler.api.mcp_server import create_server

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)  # logs go to stderr
    server = create_server(_shared(settings) if http else _local(settings))
    if http:
        server.run("streamable-http", host=host, port=port)
    else:
        server.run("stdio")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
) -> None:
    """Run the REST API (POST /v1/fetch and /v1/discover, OpenAPI docs at /docs)."""
    import uvicorn

    from progdata_crawler.api.rest import create_app

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(create_app(_shared(settings)), host=host, port=port, log_level="warning")
