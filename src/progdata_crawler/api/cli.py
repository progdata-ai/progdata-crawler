"""Command-line interface: `progdata-crawler`, also installed as `pdc`."""

from typing import Annotated

import typer

from progdata_crawler import __version__

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
