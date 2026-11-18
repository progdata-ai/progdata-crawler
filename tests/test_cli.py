import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from progdata_crawler import __version__
from progdata_crawler.api.cli import app
from progdata_crawler.settings import get_settings
from tests.support.site_server import SiteServer

runner = CliRunner()

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Remove color codes. Help output is colored when CI forces a terminal."""
    return ANSI_ESCAPE.sub("", text)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"progdata-crawler {__version__}\n"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    output = _plain(result.output)
    assert "Usage:" in output
    assert "--version" in output


@pytest.mark.parametrize("command", ["progdata-crawler", "pdc"])
def test_installed_commands(command: str) -> None:
    executable = Path(sys.executable).parent / command
    assert executable.exists(), f"{command} is not installed in the virtual environment"
    result = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    assert result.stdout == f"progdata-crawler {__version__}\n"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Give the CLI its own data directory and fresh settings."""
    monkeypatch.setenv("PDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PDC_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("cli_env")
def test_fetch_prints_markdown(test_site: SiteServer) -> None:
    result = runner.invoke(app, ["fetch", test_site.url("/article.html")])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("# How we bake our sourdough")


@pytest.mark.usefixtures("cli_env")
def test_fetch_json_envelope(test_site: SiteServer) -> None:
    result = runner.invoke(
        app, ["fetch", test_site.url("/"), "--json", "-o", "links", "-o", "metadata"]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["data"]["metadata"]["title"] == "Fjordline Bakery"
    assert len(envelope["data"]["links"]) == 5
    assert "markdown" not in envelope["data"]
    assert set(envelope["meta"]) == {"request_id", "took_ms", "cached", "rendered"}


@pytest.mark.usefixtures("cli_env")
def test_fetch_without_markdown_prints_json(test_site: SiteServer) -> None:
    result = runner.invoke(app, ["fetch", test_site.url("/"), "-o", "metadata"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["metadata"]["language"] == "en"


@pytest.mark.usefixtures("cli_env")
def test_fetch_with_selectors(test_site: SiteServer) -> None:
    result = runner.invoke(
        app, ["fetch", test_site.url("/article.html"), "--include", "article ul", "--no-store"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == (
        "- 1000 g wheat flour\n- 750 g water\n- 200 g starter\n- 22 g salt"
    )


@pytest.mark.usefixtures("cli_env")
def test_fetch_error_exits_with_1(test_site: SiteServer) -> None:
    result = runner.invoke(app, ["fetch", test_site.url("/private/staff.html")])
    assert result.exit_code == 1
    assert "robots_disallowed" in result.stderr
    assert result.stdout == ""

    as_json = runner.invoke(app, ["fetch", test_site.url("/private/staff.html"), "--json"])
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["error"]["code"] == "robots_disallowed"


@pytest.mark.usefixtures("cli_env")
def test_fetch_rejects_invalid_options() -> None:
    result = runner.invoke(app, ["fetch", "not a url"])
    assert result.exit_code == 2
    assert "Invalid options" in result.stderr
