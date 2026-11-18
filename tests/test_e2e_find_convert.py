"""End to end for phase 3: a web search that fetches its hits, and PDF, DOCX and XLSX
converted to markdown, through the REST API, the CLI and the MCP server.

Search uses the real Brave provider. The local test site plays the Brave Search API: it
answers /res/v1/web/search with hits that point at its own pages, and its robots.txt
applies to the API calls like to any other request.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner, Result

from progdata_crawler.api import rest
from progdata_crawler.api.cli import app as cli
from progdata_crawler.api.mcp_server import create_server
from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings, get_settings
from tests.support.documents import build_docx, build_xlsx
from tests.support.site_server import PDF_TEXT, SiteServer

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_TABLE = "| Flour | Grams |\n| --- | --- |\n| Rye | 300 |\n| Wheat | 700 |"


def brave_answer(site: SiteServer) -> str:
    results = [
        {"url": site.url("/article.html"), "title": "How we bake", "description": "Sourdough."},
        {"url": site.url("/files/sample.pdf"), "title": "A PDF", "description": "A document."},
        {"url": site.url("/private/staff.html"), "title": "Staff", "description": "Not for us."},
    ]
    return json.dumps({"type": "search", "web": {"results": results}})


@pytest.fixture(autouse=True)
def site(test_site: SiteServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    test_site.add("/res/v1/web/search", brave_answer(test_site), content_type="application/json")
    test_site.add("/files/notes.docx", build_docx(), content_type=DOCX)
    for name, value in {
        "PDC_DATA_DIR": str(tmp_path / "cli"),
        "PDC_RATE_LIMIT_INTERVAL_S": "0.1",
        "PDC_LOG_LEVEL": "WARNING",
        "PDC_BRAVE_API_KEY": "test-key",
        "PDC_BRAVE_API_URL": test_site.url("/res/v1"),
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def settings(tmp_path: Path, name: str) -> Settings:
    return Settings(data_dir=tmp_path / name)  # the Brave settings come from the environment


def check_find(data: dict[str, Any], site: SiteServer) -> None:
    hits = {hit["url"]: hit for hit in data["hits"]}
    assert data["provider"] == "brave"
    assert hits[site.url("/article.html")]["page"]["markdown"].startswith(
        "# How we bake our sourdough"
    )
    assert hits[site.url("/files/sample.pdf")]["page"]["markdown"] == PDF_TEXT
    assert hits[site.url("/private/staff.html")]["error"]["code"] == "robots_disallowed"


def invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args))


def test_rest(tmp_path: Path, test_site: SiteServer) -> None:
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        found = client.post("/v1/find", json={"query": "sourdough", "fetch": {}})
        pdf = client.post("/v1/convert", json={"url": test_site.url("/files/sample.pdf")})
        docx = client.post("/v1/convert/file", params={"name": "notes.docx"}, content=build_docx())
        xlsx = client.post(
            "/v1/convert/file",
            params={"name": "recipe.xlsx"},
            content=build_xlsx(),
            headers={"Content-Type": "application/octet-stream"},
        )
        text = client.post("/v1/convert/file", content=b"just text")

    check_find(found.json()["data"], test_site)
    assert pdf.json()["data"]["markdown"] == PDF_TEXT
    assert docx.json()["data"]["markdown"].startswith("# Baking notes")
    assert docx.json()["data"]["source"] == "notes.docx"
    assert xlsx.json()["data"]["markdown"].endswith(XLSX_TABLE)
    assert (text.status_code, text.json()["error"]["code"]) == (415, "unsupported_type")


def test_rest_refuses_too_large_uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "MAX_DOCUMENT_BYTES", 1000)
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        response = client.post("/v1/convert/file", content=build_docx() + b"\0" * 2000)
    assert response.status_code == 415
    assert response.json()["error"]["details"]["max_bytes"] == 1000


def test_cli(tmp_path: Path, test_site: SiteServer) -> None:
    found = invoke("find", "sourdough", "--fetch", "--json")
    assert found.exit_code == 0, found.output
    check_find(json.loads(found.stdout)["data"], test_site)

    readable = invoke("find", "sourdough", "--fetch")
    assert "# How we bake our sourdough" in readable.stdout
    assert "robots_disallowed" in readable.stdout

    workbook = tmp_path / "recipe.xlsx"
    workbook.write_bytes(build_xlsx())
    local = invoke("convert", str(workbook))
    assert local.exit_code == 0, local.output
    assert local.stdout.strip().endswith(XLSX_TABLE)

    remote = invoke("convert", test_site.url("/files/notes.docx"), "--json")
    assert json.loads(remote.stdout)["data"]["markdown"].startswith("# Baking notes")

    assert invoke("convert", str(tmp_path / "missing.pdf")).exit_code == 2
    (tmp_path / "notes.txt").write_text("plain", encoding="utf-8")
    assert invoke("convert", str(tmp_path / "notes.txt")).exit_code == 1


async def test_mcp(tmp_path: Path, test_site: SiteServer) -> None:
    async with Client(create_server(settings(tmp_path, "mcp"))) as client:
        found = await client.call_tool("pdc_find", {"query": "sourdough", "fetch": True})
        pdf = await client.call_tool("pdc_convert", {"url": test_site.url("/files/sample.pdf")})
        docx = await client.call_tool("pdc_convert", {"url": test_site.url("/files/notes.docx")})
        bad = await client.call_tool("pdc_convert", {"url": "file:///etc/passwd"})

    def data(result: Any) -> dict[str, Any]:
        return cast(dict[str, Any], result.structured_content)["data"]

    check_find(data(found), test_site)
    assert data(pdf)["markdown"] == PDF_TEXT
    assert data(docx)["markdown"].startswith("# Baking notes")
    assert cast(dict[str, Any], bad.structured_content)["error"]["code"] == "invalid_request"


async def test_same_result_everywhere(tmp_path: Path, test_site: SiteServer) -> None:
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        via_rest = client.post("/v1/find", json={"query": "sourdough"}).json()["data"]
    async with Client(create_server(settings(tmp_path, "mcp"))) as client:
        result = await client.call_tool("pdc_find", {"query": "sourdough"})
        via_mcp = cast(dict[str, Any], result.structured_content)["data"]
    cli_result = await asyncio.to_thread(invoke, "find", "sourdough", "--json")
    via_cli = json.loads(cli_result.stdout)["data"]

    def urls(data: dict[str, Any]) -> list[str]:
        return [hit["url"] for hit in data["hits"]]

    assert urls(via_rest) == urls(via_mcp) == urls(via_cli)
    assert len(urls(via_rest)) == 3


def test_search_respects_robots_txt_too(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /res/\n", content_type="text/plain")
    with TestClient(create_app(settings(tmp_path, "rest"))) as client:
        response = client.post("/v1/find", json={"query": "sourdough"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "robots_disallowed"
