"""End to end: Claude starts `progdata-crawler mcp` as a process and talks to it over stdio.

This is the phase 1 goal: pdc_fetch returns clean markdown from static and JS pages.
"""

import os
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from tests.support.site_server import SiteServer


def server_parameters(tmp_path: Path) -> StdioServerParameters:
    env = {
        "PATH": os.environ.get("PATH", ""),
        # Keep the real HOME: Playwright finds its browsers under ~/.cache.
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PDC_DATA_DIR": str(tmp_path / "data"),
        "PDC_RATE_LIMIT_INTERVAL_S": "0.1",
        "PDC_LOG_LEVEL": "WARNING",
    }
    for name in ("PDC_BROWSER_EXECUTABLE", "PLAYWRIGHT_BROWSERS_PATH"):
        if name in os.environ:
            env[name] = os.environ[name]
    return StdioServerParameters(
        command=str(Path(sys.executable).parent / "progdata-crawler"),
        args=["mcp"],
        env=env,
    )


async def fetch(client: Client, url: str) -> dict[str, Any]:
    result = await client.call_tool("pdc_fetch", {"url": url, "cache_ttl": 0})
    content: Any = result.structured_content
    assert isinstance(content, dict)
    return content  # pyright: ignore[reportUnknownVariableType]


async def test_pdc_fetch_over_stdio(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    async with Client(server_parameters(tmp_path)) as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        static = await fetch(client, test_site.url("/article.html"))
        js_page = await fetch(client, test_site.url("/js-app.html"))

    assert tools == [
        "pdc_fetch",
        "pdc_query",
        "pdc_find",
        "pdc_convert",
        "pdc_session",
        "pdc_discover",
        "pdc_harvest",
        "pdc_structure",
        "pdc_bulk",
        "pdc_job",
    ]

    assert static["error"] is None
    assert static["meta"]["rendered"] is False
    markdown = static["data"]["markdown"]
    assert markdown.startswith("# How we bake our sourdough")
    assert "## Day two: the dough" in markdown
    assert "cinnamon buns" not in markdown  # the ad is gone

    assert js_page["error"] is None
    assert js_page["meta"]["rendered"] is True
    assert js_page["data"]["markdown"] == (
        "# Order bread\n\n"
        "Choose your loaf and pick it up tomorrow morning.\n\n"
        "- Sourdough\n- Rye\n- Seeded"
    )
