import base64
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from mcp import Client
from mcp.types import TextContent

from progdata_crawler.api.mcp_server import create_server
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer


@asynccontextmanager
async def connect(tmp_path: Path, test_site: SiteServer) -> AsyncGenerator[Client]:
    """Open a client in the test's own task: the MCP client's task groups must be entered
    and exited in the same task, which a yield fixture cannot guarantee."""
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    server = create_server(settings, store_path=":memory:")
    async with Client(server) as connected:
        yield connected


async def call(client: Client, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool("pdc_fetch", arguments)
    content: Any = result.structured_content
    assert isinstance(content, dict)
    text = result.content[0]
    assert isinstance(text, TextContent)
    structured = cast(dict[str, Any], content)
    assert json.loads(text.text) == structured
    return structured


async def test_lists_the_fetch_tool(tmp_path: Path, test_site: SiteServer) -> None:
    async with connect(tmp_path, test_site) as client:
        tools = (await client.list_tools()).tools
    assert [tool.name for tool in tools] == ["pdc_fetch"]
    tool = tools[0]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert tool.input_schema["required"] == ["url"]
    expected = {"url", "outputs", "content", "include", "exclude", "render", "retain"}
    assert expected <= set(tool.input_schema["properties"])
    assert tool.output_schema is not None


async def test_fetch_defaults_to_markdown_and_metadata(
    tmp_path: Path, test_site: SiteServer
) -> None:
    async with connect(tmp_path, test_site) as client:
        envelope = await call(client, {"url": test_site.url("/article.html")})
    data = envelope["data"]
    assert data["markdown"].startswith("# How we bake our sourdough")
    assert data["metadata"]["title"] == "How we bake our sourdough"
    assert data["links"] is None
    assert envelope["error"] is None
    assert envelope["meta"]["cached"] is False


async def test_fetch_with_options(tmp_path: Path, test_site: SiteServer) -> None:
    async with connect(tmp_path, test_site) as client:
        envelope = await call(
            client,
            {
                "url": test_site.url("/article.html"),
                "outputs": ["links", "screenshot"],
                "include": ["article"],
            },
        )
    data = envelope["data"]
    assert data["markdown"] is None
    assert any(link["text"] == "frequently asked questions" for link in data["links"])
    assert base64.b64decode(data["screenshot"]).startswith(b"\x89PNG")
    assert envelope["meta"]["rendered"] is True


async def test_errors_come_back_in_the_envelope(tmp_path: Path, test_site: SiteServer) -> None:
    async with connect(tmp_path, test_site) as client:
        envelope = await call(client, {"url": test_site.url("/private/staff.html")})
    assert envelope["data"] is None
    assert envelope["error"]["code"] == "robots_disallowed"


async def test_invalid_arguments(tmp_path: Path, test_site: SiteServer) -> None:
    async with connect(tmp_path, test_site) as client:
        envelope = await call(client, {"url": "not a url"})
    assert envelope["error"]["code"] == "invalid_request"
    assert envelope["error"]["message"].startswith("url:")
