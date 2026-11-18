"""Command-line interface: `progdata-crawler`, also installed as `pdc`."""

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

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
from progdata_crawler.core.find import create_provider, find
from progdata_crawler.core.jobs.events import job_events
from progdata_crawler.core.jobs.handlers import job_handlers
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.notify import create_notifier
from progdata_crawler.core.research.agent import research
from progdata_crawler.core.store.query import query_pages
from progdata_crawler.core.structure.llm import LlmAnswer, create_llm
from progdata_crawler.core.structure.service import structure
from progdata_crawler.core.watch.scheduler import WatchService
from progdata_crawler.log import configure_logging
from progdata_crawler.models import (
    BulkRequest,
    ContentMode,
    ConvertResult,
    Device,
    DiscoverRequest,
    DiscoverResult,
    Envelope,
    FetchRequest,
    FindRequest,
    FindResult,
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
    SearchSource,
    TimeRange,
)
from progdata_crawler.models.research import ResearchAnswer, ResearchRequest
from progdata_crawler.models.structure_request import StructureRequest, StructureResult
from progdata_crawler.models.watch import CheckStatus, Watch, WatchCheck, WatchCreate
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
    steps: Annotated[
        Path | None,
        typer.Option(help="JSON file with browser steps to run after load (a list)."),
    ] = None,
    structure: Annotated[
        Path | None,
        typer.Option(help='JSON file with {"schema": ..., "instruction": ...} for -o structured.'),
    ] = None,
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
    if steps is not None:
        fields["steps"] = _read_json(steps)
    if structure is not None:
        fields["structure"] = _read_json(structure)
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
    goal: Annotated[
        str | None,
        typer.Option(help="What you are looking for; the LLM ranks links to fetch the best first."),
    ] = None,
    goal_budget: Annotated[int, typer.Option(help="Most LLM calls for ranking links.")] = 10,
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
    """Crawl a site (breadth first, or guided by --goal), printing each page as it finishes."""
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
                "goal": goal,
                "goal_budget": goal_budget,
                **_page_options(output, content, render, no_store),
            }
        )
    except ValidationError as error:
        raise _invalid_options(error) from error
    _finish(asyncio.run(_run_job(_local(settings), JobKind.HARVEST, request, as_json=as_json)))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        typer.echo(f"Cannot read {path}: {error}", err=True)
        raise typer.Exit(2) from error


@app.command(name="structure")
def structure_command(
    urls: Annotated[list[str], typer.Argument(help="The pages to extract from.")],
    schema: Annotated[Path, typer.Option(help="JSON file with the JSON Schema of the result.")],
    instruction: Annotated[str, typer.Option(help="What to extract, in plain words.")] = "",
    render: Annotated[RenderMode, typer.Option(help="When to use a browser.")] = RenderMode.AUTO,
) -> None:
    """Extract data that fits a JSON Schema from pages, with the LLM. Prints JSON: the data
    for one URL, or one line per page for several."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    fields: dict[str, Any] = {
        "structure": {"schema": _read_json(schema), "instruction": instruction},
        "outputs": [Output.STRUCTURED],
        "render": render,
    }
    try:
        request = StructureRequest.model_validate({**fields, "urls": urls})
    except ValidationError as error:
        raise _invalid_options(error) from error
    if len(urls) == 1:
        envelope = asyncio.run(_structure_one(_local(settings), request))
        if envelope.error is not None:
            typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
            raise typer.Exit(1)
        assert envelope.data is not None
        typer.echo(json.dumps(envelope.data.data, indent=2, ensure_ascii=False))
        return
    bulk_request = BulkRequest.model_validate({**fields, "urls": urls})
    _finish(asyncio.run(_run_job(_local(settings), JobKind.BULK, bulk_request, as_json=True)))


async def _structure_one(
    settings: Settings, request: StructureRequest
) -> Envelope[StructureResult]:
    async with Crawler(settings) as crawler:
        return await enveloped(StructureResult, structure(crawler, None, request))


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


@app.command(name="find")
def find_command(
    query: Annotated[str, typer.Argument(help="What to search for.")],
    source: Annotated[
        list[SearchSource] | None,
        typer.Option("--source", "-s", help="web, news or images. Repeatable. Default: web."),
    ] = None,
    time_range: Annotated[
        TimeRange | None, typer.Option(help="Only results from the last day/week/month/year.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Most results per source.")] = 10,
    country: Annotated[str | None, typer.Option(help="Country code, e.g. NO.")] = None,
    language: Annotated[str | None, typer.Option(help="Language code, e.g. nb.")] = None,
    fetch: Annotated[
        bool, typer.Option("--fetch", help="Also fetch every web and news hit.")
    ] = False,
    output: OutputOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """Search the web; with --fetch also fetch the hits."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        request = FindRequest.model_validate(
            {
                "query": query,
                "sources": source or [SearchSource.WEB],
                "time_range": time_range,
                "limit": limit,
                "country": country,
                "language": language,
                "fetch": {"outputs": output or [Output.MARKDOWN]} if fetch else None,
            }
        )
    except ValidationError as error:
        raise _invalid_options(error) from error

    envelope = asyncio.run(_find(_local(settings), request))
    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        for hit in envelope.data.hits:
            typer.echo(f"{hit.title}\n  {hit.url}" + (f"\n  {hit.snippet}" if hit.snippet else ""))
            if hit.error is not None:
                typer.echo(f"  ✗ {hit.error.code.value}: {hit.error.message}")
            elif hit.page is not None and hit.page.markdown:
                typer.echo("\n" + hit.page.markdown)
            typer.echo("")
    if envelope.error is not None:
        raise typer.Exit(1)


async def _find(settings: Settings, request: FindRequest) -> Envelope[FindResult]:
    async with Crawler(settings) as crawler:
        provider = create_provider(settings, robots=crawler.check_robots, store=crawler.store)
        try:
            return await enveloped(FindResult, find(crawler, provider, request))
        finally:
            await provider.aclose()


@app.command()
def convert(
    target: Annotated[str, typer.Argument(help="A URL, or the path of a local file.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """Convert a PDF, DOCX or XLSX document (URL or local file) to markdown."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if not target.startswith(("http://", "https://")) and not Path(target).is_file():
        typer.echo(f"No such file: {target}", err=True)
        raise typer.Exit(2)
    envelope = asyncio.run(_convert(_local(settings), target))
    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        typer.echo(envelope.data.markdown)
    if envelope.error is not None:
        raise typer.Exit(1)


async def _convert(settings: Settings, target: str) -> Envelope[ConvertResult]:
    if target.startswith(("http://", "https://")):
        async with Crawler(settings) as crawler:
            return await enveloped(ConvertResult, convert_url(crawler, target))
    path = Path(target)
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        return await enveloped(
            ConvertResult,
            convert_bytes(b"\0" * (MAX_DOCUMENT_BYTES + 1), source=path.name),
        )
    content = await asyncio.to_thread(path.read_bytes)
    return await enveloped(ConvertResult, convert_bytes(content, source=path.name, name=path.name))


LLM_CHECK_PROMPT = "Answer with one word: what colour is a clear daytime sky?"


@app.command(name="llm-check")
def llm_check() -> None:
    """Send one short question to the configured LLM, to check that it works."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        answer = asyncio.run(_llm_check(_local(settings)))
    except CrawlerError as error:
        typer.echo(f"Error ({error.code.value}): {error.message}", err=True)
        raise typer.Exit(1) from error
    typer.echo(f"Provider: {settings.llm_provider.value if settings.llm_provider else '-'}")
    typer.echo(f"Model:    {answer.model}")
    typer.echo(f"Answer:   {answer.output.strip()}")
    typer.echo(f"Tokens:   {answer.usage.input_tokens} in, {answer.usage.output_tokens} out")


async def _llm_check(settings: Settings) -> LlmAnswer[str]:
    async with Crawler(settings) as crawler:
        llm = create_llm(settings, robots=crawler.check_robots)
        return await llm.run(
            instructions="You are a test. Keep the answer short.",
            prompt=LLM_CHECK_PROMPT,
            output_type=str,
        )


watch_app = typer.Typer(help="Watch pages for changes.", no_args_is_help=True)
app.add_typer(watch_app, name="watch")


def _print_check(check: WatchCheck) -> None:
    line = f"{check.status.value:9} {check.url}"
    if check.status is CheckStatus.CHANGED:
        line += f"  (+{check.added_lines} -{check.removed_lines})"
    if check.error is not None:
        line += f"  {check.error.code.value}: {check.error.message}"
    typer.echo(line)
    if check.diff:
        typer.echo(check.diff)


def _print_watch(watch: Watch) -> None:
    status = watch.last_status.value if watch.last_status else "not checked"
    typer.echo(f"{watch.id}  every {watch.interval_s} s  {status:11}  {watch.url}")


async def _with_watches[T](
    settings: Settings, work: Callable[[WatchService], Awaitable[T]], *, schedule: bool = False
) -> T:
    async with (
        Crawler(settings) as crawler,
        WatchService(crawler, create_notifier(settings), schedule=schedule) as watches,
    ):
        return await work(watches)


def _watch_settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return _local(settings)


def _run_watch_command[T](work: Callable[[WatchService], Awaitable[T]]) -> T:
    try:
        return asyncio.run(_with_watches(_watch_settings(), work))
    except CrawlerError as error:
        typer.echo(f"Error ({error.code.value}): {error.message}", err=True)
        raise typer.Exit(1) from error


@watch_app.command("add")
def watch_add(
    url: Annotated[str, typer.Argument(help="The page to watch.")],
    every: Annotated[int, typer.Option(help="Seconds between checks (at least 300).")] = 3600,
    notify_url: Annotated[
        str | None, typer.Option(help="Receives a signed POST with the diff on change.")
    ] = None,
    output: OutputOption = None,
) -> None:
    """Start watching a page; it is checked right away."""
    fields: dict[str, Any] = {"url": url, "interval_s": every, "notify_url": notify_url}
    if output:
        fields["outputs"] = output
    try:
        request = WatchCreate.model_validate(fields)
    except ValidationError as error:
        raise _invalid_options(error) from error

    async def work(watches: WatchService) -> WatchCheck:
        watch = await watches.add(request)
        typer.echo(f"Watching {watch.url} as {watch.id}")
        return await watches.check(watch.id)

    _print_check(_run_watch_command(work))


@watch_app.command("list")
def watch_list() -> None:
    """List the watches."""
    for watch in _run_watch_command(lambda watches: watches.all()):
        _print_watch(watch)


@watch_app.command("check")
def watch_check(
    watch_id: Annotated[
        str | None, typer.Argument(help="Check this watch now. Without it: every due watch.")
    ] = None,
) -> None:
    """Check watches once: one by id, or all that are due (for cron)."""

    async def work(watches: WatchService) -> list[WatchCheck]:
        if watch_id is not None:
            return [await watches.check(watch_id)]
        return await watches.check_due()

    checks = _run_watch_command(work)
    if not checks:
        typer.echo("No watches are due.", err=True)
    for check in checks:
        _print_check(check)


@watch_app.command("remove")
def watch_remove(watch_id: Annotated[str, typer.Argument(help="The watch to remove.")]) -> None:
    """Stop watching a page and delete the versions kept for it."""
    watch = _run_watch_command(lambda watches: watches.remove(watch_id))
    typer.echo(f"Removed {watch.id} ({watch.url})")


@watch_app.command("run")
def watch_run() -> None:
    """Keep checking watches as they fall due, until stopped with Ctrl+C."""

    async def forever(watches: WatchService) -> None:
        typer.echo("Checking watches as they fall due. Stop with Ctrl+C.", err=True)
        await asyncio.Event().wait()

    settings = _watch_settings()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_with_watches(settings, forever, schedule=True))


@app.command(name="research")
def research_command(
    question: Annotated[str, typer.Argument(help="The question to answer.")],
    max_pages: Annotated[int, typer.Option(help="Most pages to read.")] = 8,
    max_rounds: Annotated[int, typer.Option(help="Rounds of searching.")] = 2,
    max_seconds: Annotated[int, typer.Option(help="Time for searching and reading.")] = 120,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full response envelope as JSON.")
    ] = False,
) -> None:
    """Answer a question from the web, with the sources it read."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        request = ResearchRequest(
            question=question,
            max_pages=max_pages,
            max_rounds=max_rounds,
            max_seconds=max_seconds,
        )
    except ValidationError as error:
        raise _invalid_options(error) from error
    envelope = asyncio.run(_research(_local(settings), request))
    if as_json:
        typer.echo(envelope.model_dump_json(indent=2, exclude_none=True))
    elif envelope.error is not None:
        typer.echo(f"Error ({envelope.error.code.value}): {envelope.error.message}", err=True)
    elif envelope.data is not None:
        result = envelope.data
        typer.echo(result.answer.strip())
        if result.sources:
            typer.echo("\nSources:")
            for source in result.sources:
                typer.echo(f"[{source.number}] {source.title} - {source.url}")
        for note in result.notes:
            typer.echo(f"Note: {note}", err=True)
    if envelope.error is not None:
        raise typer.Exit(1)


async def _research(settings: Settings, request: ResearchRequest) -> Envelope[ResearchAnswer]:
    async with Crawler(settings) as crawler:
        provider = create_provider(settings, robots=crawler.check_robots, store=crawler.store)
        try:
            return await enveloped(ResearchAnswer, research(crawler, provider, request))
        finally:
            await provider.aclose()


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
