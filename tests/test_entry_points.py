"""The same discover, harvest and bulk call through REST, MCP and the CLI gives the same
result."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner, Result

from progdata_crawler.api.cli import app as cli
from progdata_crawler.api.mcp_server import create_server
from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings, get_settings
from tests.support.site_server import PUBLIC_PAGES, SiteServer

ROBOTS = "User-agent: *\nDisallow: /private/\n"

runner = CliRunner()


@pytest.fixture(autouse=True)
def site(test_site: SiteServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    test_site.add("/robots.txt", ROBOTS, content_type="text/plain")
    monkeypatch.setenv("PDC_DATA_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("PDC_RATE_LIMIT_INTERVAL_S", "0.1")
    monkeypatch.setenv("PDC_LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def settings(tmp_path: Path, name: str) -> Settings:
    return Settings(data_dir=tmp_path / name, rate_limit_interval_s=0.1)


def rest(tmp_path: Path, path: str, body: dict[str, Any], **params: Any) -> dict[str, Any]:
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        response = client.post(path, json=body, params=params)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json()["data"])


async def mcp(tmp_path: Path, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with Client(create_server(settings(tmp_path, "mcp"))) as client:
        result = await client.call_tool(tool, arguments)
    content = result.structured_content
    assert isinstance(content, dict)
    envelope = cast(dict[str, Any], content)
    assert envelope["error"] is None, envelope["error"]
    return cast(dict[str, Any], envelope["data"])


def invoke(*args: str) -> Result:
    return runner.invoke(cli, list(args))


async def invoke_async(*args: str) -> Result:
    """The CLI runs its own event loop, so async tests call it from a thread."""
    return await asyncio.to_thread(invoke, *args)


async def cli_lines_async(*args: str) -> list[dict[str, Any]]:
    return parse_lines(await invoke_async(*args, "--json"))


def cli_lines(*args: str) -> list[dict[str, Any]]:
    return parse_lines(invoke(*args, "--json"))


def parse_lines(result: Result) -> list[dict[str, Any]]:
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def without_nulls(value: Any) -> Any:
    """The CLI leaves out null fields; REST and MCP send them."""
    if isinstance(value, dict):
        items = cast(dict[str, Any], value).items()
        return {key: without_nulls(item) for key, item in items if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in cast(list[Any], value)]
    return value


def pages(results: list[dict[str, Any]]) -> dict[str, tuple[str | None, str | None]]:
    """url -> (markdown, title), the parts that must be the same everywhere."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for result in results:
        page = cast(dict[str, Any], result.get("page") or {})
        metadata = cast(dict[str, Any], page.get("metadata") or {})
        out[result["url"]] = (page.get("markdown"), metadata.get("title"))
    return out


async def test_discover(tmp_path: Path, test_site: SiteServer) -> None:
    start = test_site.url("/")
    via_rest = rest(tmp_path, "/v1/discover", {"url": start, "paths": {"include": ["^/docs/"]}})
    via_mcp = await mcp(tmp_path, "pdc_discover", {"url": start, "include_paths": ["^/docs/"]})
    result = await invoke_async("discover", start, "--include-path", "^/docs/", "--json")
    assert result.exit_code == 0, result.output
    via_cli = json.loads(result.stdout)["data"]

    assert without_nulls(via_rest) == without_nulls(via_mcp) == via_cli
    assert len(via_rest["urls"]) == len([p for p in PUBLIC_PAGES if p.startswith("/docs/")])


async def test_harvest(tmp_path: Path, test_site: SiteServer) -> None:
    start = test_site.url("/docs/")
    body = {"url": start, "max_depth": 1, "paths": {"include": ["^/docs/"]}}
    via_rest = rest(tmp_path, "/v1/harvest", body, wait_s=60)
    via_mcp = await mcp(
        tmp_path, "pdc_harvest", {"url": start, "max_depth": 1, "include_paths": ["^/docs/"]}
    )
    via_cli = await cli_lines_async(
        "harvest", start, "--max-depth", "1", "--include-path", "^/docs/"
    )

    for view in (via_rest, via_mcp):
        assert view["job"]["status"] == "succeeded"
        assert view["job"]["kind"] == "harvest"
        assert view["complete"] is True
    expected = pages(via_rest["results"])
    assert set(expected) == {test_site.url(p) for p in PUBLIC_PAGES if p.startswith("/docs/")}
    assert pages(via_mcp["results"]) == expected
    assert pages(via_cli) == expected


async def test_bulk(tmp_path: Path, test_site: SiteServer) -> None:
    urls = [test_site.url("/article.html"), test_site.url("/private/staff.html")]
    via_rest = rest(tmp_path, "/v1/bulk", {"urls": urls}, wait_s=60)
    via_mcp = await mcp(tmp_path, "pdc_bulk", {"urls": urls})
    via_cli = await cli_lines_async("bulk", *urls)

    expected = pages(via_rest["results"])
    assert set(expected) == set(urls)
    assert pages(via_mcp["results"]) == pages(via_cli) == expected
    errors = {r["url"]: r["error"]["code"] for r in via_rest["results"] if r.get("error")}
    assert errors == {test_site.url("/private/staff.html"): "robots_disallowed"}


def test_rest_returns_a_running_job_without_wait(tmp_path: Path, test_site: SiteServer) -> None:
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        started = client.post("/v1/harvest", json={"url": test_site.url("/")}).json()["data"]
        assert started["job"]["status"] in ("queued", "running")
        job_id = started["job"]["id"]
        with client.stream("GET", f"/v1/jobs/{job_id}/events") as events:
            events.read()
        done = client.get(f"/v1/jobs/{job_id}").json()["data"]
    assert done["job"]["status"] == "succeeded"
    assert len(done["results"]) == len(PUBLIC_PAGES)


async def test_mcp_job_tool_pages_and_cancels(tmp_path: Path, test_site: SiteServer) -> None:
    async with Client(create_server(settings(tmp_path, "mcp"))) as client:
        started = await client.call_tool(
            "pdc_harvest", {"url": test_site.url("/"), "wait_s": 0, "limit": 1}
        )
        job_id = cast(dict[str, Any], started.structured_content)["data"]["job"]["id"]

        collected: list[str] = []
        cursor = 0
        while True:
            answer = await client.call_tool(
                "pdc_job", {"job_id": job_id, "cursor": cursor, "limit": 3}
            )
            view = cast(dict[str, Any], answer.structured_content)["data"]
            collected += [result["url"] for result in view["results"]]
            cursor = view["next_cursor"]
            if view["complete"]:
                break
        assert sorted(collected) == sorted(test_site.url(p) for p in PUBLIC_PAGES)

        slow = await client.call_tool(
            "pdc_bulk", {"urls": [test_site.url("/slow?ms=3000")], "wait_s": 0}
        )
        slow_id = cast(dict[str, Any], slow.structured_content)["data"]["job"]["id"]
        cancelled = await client.call_tool("pdc_job", {"job_id": slow_id, "cancel": True})
        view = cast(dict[str, Any], cancelled.structured_content)["data"]
        assert view["job"]["status"] == "cancelled"

        missing = await client.call_tool("pdc_job", {"job_id": "nope"})
        error = cast(dict[str, Any], missing.structured_content)["error"]
        assert error["code"] == "not_found"


def test_cli_job_reads_a_finished_job(test_site: SiteServer) -> None:
    result = runner.invoke(cli, ["bulk", test_site.url("/article.html")])
    assert result.exit_code == 0, result.output
    job_id = result.stderr.splitlines()[0].removeprefix("Job ")

    shown = runner.invoke(cli, ["job", job_id])
    assert shown.exit_code == 0, shown.output
    assert "succeeded" in shown.stderr
    assert test_site.url("/article.html") in shown.stdout

    as_json = json.loads(runner.invoke(cli, ["job", job_id, "--json"]).stdout)
    assert as_json["data"]["job"]["id"] == job_id

    assert runner.invoke(cli, ["job", "nope"]).exit_code == 1


def test_cli_bulk_reads_urls_from_a_file(tmp_path: Path, test_site: SiteServer) -> None:
    file = tmp_path / "urls.txt"
    file.write_text(f"{test_site.url('/')}\n\n{test_site.url('/docs/')}\n", encoding="utf-8")
    lines = cli_lines("bulk", "--file", str(file))
    assert {line["url"] for line in lines} == {test_site.url("/"), test_site.url("/docs/")}


def test_cli_harvest_fails_when_the_start_page_fails(test_site: SiteServer) -> None:
    result = runner.invoke(cli, ["harvest", test_site.url("/private/staff.html")])
    assert result.exit_code == 1
    assert "robots_disallowed" in result.stderr
