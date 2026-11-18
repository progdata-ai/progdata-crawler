"""Local search end to end: harvest the test site, then find over what was fetched through
the REST API, the CLI and the MCP server, with PDC_SEARCH_PROVIDER=local."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from progdata_crawler.api.cli import app as cli
from progdata_crawler.api.mcp_server import create_server
from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings, get_settings
from tests.support.site_server import SiteServer

runner = CliRunner()


@pytest.fixture(autouse=True)
def harvested(
    test_site: SiteServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    for name, value in {
        "PDC_DATA_DIR": str(tmp_path),
        "PDC_RATE_LIMIT_INTERVAL_S": "0.1",
        "PDC_LOG_LEVEL": "WARNING",
        "PDC_SEARCH_PROVIDER": "local",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    result = runner.invoke(cli, ["harvest", test_site.url("/"), "--max-depth", "2"])
    assert result.exit_code == 0, result.output
    # From here on nothing may be fetched: the search itself must not need the network.
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /\n", content_type="text/plain")
    yield
    get_settings.cache_clear()


def urls(data: dict[str, Any]) -> list[str]:
    return [hit["url"] for hit in data["hits"]]


async def test_find_over_harvested_pages_everywhere(test_site: SiteServer) -> None:
    settings = Settings()
    with TestClient(create_app(settings)) as client:
        via_rest = client.post("/v1/find", json={"query": "sourdough"}).json()["data"]
    async with Client(create_server(settings)) as client:
        result = await client.call_tool("pdc_find", {"query": "sourdough"})
        via_mcp = cast(dict[str, Any], result.structured_content)["data"]
    cli_result = await asyncio.to_thread(runner.invoke, cli, ["find", "sourdough", "--json"])
    assert cli_result.exit_code == 0, cli_result.output
    via_cli = json.loads(cli_result.stdout)["data"]

    assert via_rest["provider"] == "local"
    assert test_site.url("/article.html") in urls(via_rest)
    assert urls(via_rest) == urls(via_mcp) == urls(via_cli)
    hit = via_rest["hits"][0]
    assert hit["title"] and "[" in hit["snippet"]


def test_find_with_fetch_still_respects_robots_txt(test_site: SiteServer) -> None:
    request = {"query": "sourdough", "fetch": {"outputs": ["markdown"]}}
    with TestClient(create_app(Settings())) as client:
        refused = client.post("/v1/find", json=request).json()["data"]
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    with TestClient(create_app(Settings())) as client:  # a fresh robots.txt cache
        allowed = client.post("/v1/find", json=request).json()["data"]

    def article(data: dict[str, Any]) -> dict[str, Any]:
        return next(hit for hit in data["hits"] if hit["url"] == test_site.url("/article.html"))

    # The search is local, but fetching a hit is a request like any other.
    assert article(refused)["error"]["code"] == "robots_disallowed"
    assert article(allowed)["page"]["markdown"].startswith("# How we bake our sourdough")


def test_news_gives_a_note_not_an_error() -> None:
    with TestClient(create_app(Settings())) as client:
        response = client.post("/v1/find", json={"query": "sourdough", "sources": ["news"]})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["hits"] == []
    assert data["notes"] == ["the local search provider has no news index"]
