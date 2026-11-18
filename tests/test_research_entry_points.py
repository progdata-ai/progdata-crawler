"""Research through REST and MCP, with the local search provider and a fake LLM: the
answer's sources are pages that were actually fetched."""

import re
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from mcp import Client

from progdata_crawler.api.mcp_server import create_server
from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.site_server import SiteServer

SOURCE_LINE = re.compile(r"^\[(\d+)\] ", re.MULTILINE)


def fake_researcher() -> FakeLlm:
    def reply(prompt: str) -> dict[str, Any]:
        if "Sources:" in prompt:
            numbers = SOURCE_LINE.findall(prompt)
            cited = " ".join(f"[{n}]" for n in numbers)
            return {"answer": f"Bake for three days {cited} [99].", "missing": []}
        return {"queries": ["sourdough"]}

    return FakeLlm(reply)


def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, rate_limit_interval_s=0.1, search_provider="local")  # pyright: ignore[reportArgumentType]


def test_rest(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    app = create_app(settings(tmp_path), llm=fake_researcher().llm())
    with TestClient(app) as client:
        client.post("/v1/fetch", json={"url": test_site.url("/article.html")})
        response = client.post("/v1/research", json={"question": "How long does sourdough take?"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert [s["url"] for s in data["sources"]] == [test_site.url("/article.html")]
    assert "[99]" not in data["answer"]
    assert data["usage"]["pages"] >= 1


async def test_mcp(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    server = create_server(settings(tmp_path), llm=fake_researcher().llm())
    async with Client(server) as client:
        await client.call_tool("pdc_fetch", {"url": test_site.url("/article.html")})
        result = await client.call_tool(
            "pdc_research", {"question": "How long does sourdough take?"}
        )
    envelope = cast(dict[str, Any], result.structured_content)
    assert envelope["error"] is None
    assert [s["url"] for s in envelope["data"]["sources"]] == [test_site.url("/article.html")]
