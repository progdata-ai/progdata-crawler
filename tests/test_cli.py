import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from progdata_crawler import __version__
from progdata_crawler.api.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"progdata-crawler {__version__}\n"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage:" in result.output
    assert "--version" in result.output


@pytest.mark.parametrize("command", ["progdata-crawler", "pdc"])
def test_installed_commands(command: str) -> None:
    executable = Path(sys.executable).parent / command
    assert executable.exists(), f"{command} is not installed in the virtual environment"
    result = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    assert result.stdout == f"progdata-crawler {__version__}\n"
