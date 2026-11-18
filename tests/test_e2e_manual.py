"""End to end: crawl the generated 100-page manual through the real entry points, with a
path filter and a depth limit. This is phase 2's done criterion."""

import json
import time
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
from tests.support.manual_site import MANUAL_PATHS, ROBOTS, paths_up_to_depth
from tests.support.site_server import SiteServer

ONLY_MANUAL = "^/manual/"


@pytest.fixture(autouse=True)
def manual(
    test_site: SiteServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    robots = ROBOTS.format(sitemap=test_site.url("/manual/sitemap.xml"))
    test_site.add("/robots.txt", robots, content_type="text/plain")
    monkeypatch.setenv("PDC_DATA_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("PDC_RATE_LIMIT_INTERVAL_S", "0.1")
    monkeypatch.setenv("PDC_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def settings(tmp_path: Path, name: str) -> Settings:
    return Settings(data_dir=tmp_path / name, rate_limit_interval_s=0.1)


def assert_clean_crawl(results: list[dict[str, Any]], expected: set[str], site: SiteServer) -> None:
    urls = [result["url"] for result in results]
    assert len(urls) == len(set(urls)), "duplicates"
    assert set(urls) == {site.url(path) for path in expected}
    assert all(result.get("error") is None for result in results)
    assert not any("/manual/internal/" in url or "/blog/" in url for url in urls)


def test_rest_crawls_the_whole_manual(tmp_path: Path, test_site: SiteServer) -> None:
    body = {
        "url": test_site.url("/manual/"),
        "paths": {"include": [ONLY_MANUAL]},
        "max_depth": 3,
        "max_pages": 1000,
        "sitemaps": False,
    }
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        began = time.monotonic()
        started = client.post("/v1/harvest", json=body).json()["data"]
        job_id = started["job"]["id"]
        with client.stream("GET", f"/v1/jobs/{job_id}/events") as events:
            events.read()  # until the job has finished
        took = time.monotonic() - began
        results: list[dict[str, Any]] = []
        cursor = 0
        while True:
            view = client.get(f"/v1/jobs/{job_id}", params={"cursor": cursor, "limit": 30}).json()[
                "data"
            ]
            results += view["results"]
            cursor = view["next_cursor"]
            if view["complete"]:
                break

    assert view["job"]["status"] == "succeeded"
    assert view["job"]["progress"] == {"total": 100, "done": 100, "failed": 0}
    # One host, at most one request per 0.1 s: the crawl cannot be faster than this.
    assert took >= 100 * 0.1 * 0.95
    assert_clean_crawl(results, set(MANUAL_PATHS), test_site)
    by_url = {result["url"]: result["page"] for result in results}
    page = by_url[test_site.url("/manual/operate/reference/page-4.html")]
    assert "unique text operatereference4" in page["markdown"]
    assert page["metadata"]["title"] == "Operate: Reference 4"


def test_cli_stops_at_max_depth(test_site: SiteServer) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "harvest",
            test_site.url("/manual/"),
            "--include-path",
            ONLY_MANUAL,
            "--max-depth",
            "2",
            "--no-sitemaps",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    results = [json.loads(line) for line in result.stdout.splitlines()]
    assert_clean_crawl(results, paths_up_to_depth(2), test_site)
    assert len(results) == 16


async def test_mcp_discovers_and_crawls_from_the_sitemap(
    tmp_path: Path, test_site: SiteServer
) -> None:
    async with Client(create_server(settings(tmp_path, "mcp"))) as client:
        found = await client.call_tool(
            "pdc_discover",
            {"url": test_site.url("/manual/"), "include_paths": [ONLY_MANUAL], "links": False},
        )
        crawled = await client.call_tool(
            "pdc_harvest",
            {
                "url": test_site.url("/manual/"),
                "include_paths": [ONLY_MANUAL],
                "max_depth": 1,
                "max_pages": 30,
                "outputs": ["metadata"],
                "wait_s": 60,
            },
        )
    discovered = cast(dict[str, Any], found.structured_content)["data"]
    assert [item["url"] for item in discovered["urls"]] == [
        test_site.url(path) for path in MANUAL_PATHS
    ]
    assert discovered["sitemaps"] == [test_site.url("/manual/sitemap.xml")]

    view = cast(dict[str, Any], crawled.structured_content)["data"]
    assert view["job"]["status"] == "succeeded" and view["complete"]
    results = view["results"]
    # Sitemap URLs count as depth 1, so a depth of 1 reaches deep pages, within max_pages.
    assert len(results) == 30
    urls = [result["url"] for result in results]
    assert len(set(urls)) == 30
    assert all(url.startswith(test_site.url("/manual/")) for url in urls)
    assert any(url.endswith(".html") for url in urls)
    assert all(result["page"]["markdown"] is None for result in results)
