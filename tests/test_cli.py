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
from progdata_crawler.settings import Settings, get_settings
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


def test_network_defaults_per_entry_point() -> None:
    from progdata_crawler.api.cli import _local, _shared  # pyright: ignore[reportPrivateUsage]

    unset = Settings(allow_private_networks=None)
    assert _local(unset).allow_private_networks is True
    assert _shared(unset).allow_private_networks is False
    explicit = Settings(allow_private_networks=False)
    assert _local(explicit).allow_private_networks is False


@pytest.mark.usefixtures("cli_env")
def test_discover_prints_urls(test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    result = runner.invoke(
        app, ["discover", test_site.url("/"), "--include-path", "^/docs/", "--no-links"]
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert test_site.url("/docs/faq.html") in lines
    assert all("/docs/" in line for line in lines)


@pytest.mark.usefixtures("cli_env")
def test_discover_json(test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    result = runner.invoke(app, ["discover", test_site.url("/"), "--limit", "2", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert len(body["data"]["urls"]) == 2
    assert body["data"]["truncated"] is True


@pytest.mark.usefixtures("cli_env")
def test_query_searches_fetched_pages(test_site: SiteServer) -> None:
    assert runner.invoke(app, ["fetch", test_site.url("/article.html")]).exit_code == 0
    result = runner.invoke(app, ["query", "sourdough", "bake"])
    assert result.exit_code == 0, result.output
    assert test_site.url("/article.html") in result.stdout
    assert "How we bake our sourdough" in result.stdout

    as_json = json.loads(runner.invoke(app, ["query", "sourdough", "--json"]).stdout)
    assert as_json["data"]["hits"][0]["url"] == test_site.url("/article.html")

    none = runner.invoke(app, ["query", "zebra"])
    assert none.exit_code == 0 and none.stdout == ""


CHAT_COMPLETION = {
    "id": "chat-1",
    "object": "chat.completion",
    "created": 0,
    "model": "small",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Blue"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 21, "completion_tokens": 1, "total_tokens": 22},
}


@pytest.mark.usefixtures("cli_env")
def test_llm_check_against_an_ollama_style_server(
    test_site: SiteServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_site.add(
        "/v1/chat/completions", json.dumps(CHAT_COMPLETION), content_type="application/json"
    )
    monkeypatch.setenv("PDC_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("PDC_LLM_MODEL", "small")
    monkeypatch.setenv("PDC_OLLAMA_URL", test_site.url("/v1"))
    get_settings.cache_clear()

    result = runner.invoke(app, ["llm-check"])
    assert result.exit_code == 0, result.output
    assert "Answer:   Blue" in result.stdout
    assert "Tokens:   21 in, 1 out" in result.stdout
    sent = json.loads(test_site.received[-1].body)
    assert sent["model"] == "small"
    assert "colour" in sent["messages"][-1]["content"]


@pytest.mark.usefixtures("cli_env")
def test_llm_check_without_configuration() -> None:
    result = runner.invoke(app, ["llm-check"])
    assert result.exit_code == 1
    assert "llm_unavailable" in result.stderr
    assert "PDC_LLM_PROVIDER" in result.stderr
