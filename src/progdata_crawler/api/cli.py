"""Command-line interface: `progdata-crawler`, also installed as `pdc`."""

import asyncio
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from progdata_crawler import __version__
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.log import configure_logging
from progdata_crawler.models import (
    ContentMode,
    Device,
    Envelope,
    FetchRequest,
    Output,
    Page,
    RenderMode,
    Retention,
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

    envelope = asyncio.run(_fetch(settings, request))

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


def _human_readable(page: Page) -> str:
    """Markdown when we have it; otherwise the other outputs as JSON."""
    if page.markdown is not None:
        return page.markdown
    return page.model_dump_json(indent=2, exclude_none=True)
