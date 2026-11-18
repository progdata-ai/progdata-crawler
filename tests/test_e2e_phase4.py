"""End to end for phase 4: extraction against a schema gives valid JSON, and an agent
clicks its way through a form, through the REST API, the CLI and the MCP server.

The LLM is the real Ollama provider code talking to the local test site, which answers
/v1/chat/completions the way an Ollama server does, with a tool call that carries the
extracted data. No real model is called.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate
from mcp import Client
from typer.testing import CliRunner, Result

from progdata_crawler.api.cli import app as cli
from progdata_crawler.api.mcp_server import create_server
from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings, get_settings
from tests.support.site_server import SiteServer

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"heading": {"type": "string"}, "days": {"type": "integer"}},
    "required": ["heading", "days"],
}
EXTRACTED = {"heading": "How we bake our sourdough", "days": 3}
THANKS = (
    "<html><head><title>Thanks</title></head><body><h1>Thank you for your order</h1></body></html>"
)
FORM_STEPS = [
    {"action": "type", "selector": "#name", "text": "Ada"},
    {"action": "click", "selector": "#send"},
]


def tool_call_completion() -> str:
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "final_result", "arguments": json.dumps(EXTRACTED)},
    }
    return json.dumps(
        {
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": "small",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }
    )


@pytest.fixture(autouse=True)
def site(test_site: SiteServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    test_site.add("/submit", THANKS)
    test_site.add("/v1/chat/completions", tool_call_completion(), content_type="application/json")
    for name, value in {
        "PDC_DATA_DIR": str(tmp_path / "data"),
        "PDC_RATE_LIMIT_INTERVAL_S": "0.1",
        "PDC_LOG_LEVEL": "WARNING",
        "PDC_LLM_PROVIDER": "ollama",
        "PDC_LLM_MODEL": "small",
        "PDC_OLLAMA_URL": test_site.url("/v1"),
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args))


def test_rest(test_site: SiteServer) -> None:
    with TestClient(create_app(Settings())) as client:
        extracted = client.post(
            "/v1/structure",
            json={"urls": [test_site.url("/article.html")], "structure": {"schema": SCHEMA}},
        ).json()["data"]["data"]

        session = client.post("/v1/session", json={"url": test_site.url("/form.html")}).json()
        session_id = session["data"]["id"]
        after = client.post(f"/v1/session/{session_id}/step", json={"steps": FORM_STEPS}).json()
        client.delete(f"/v1/session/{session_id}")

    validate(extracted, SCHEMA)
    assert extracted == EXTRACTED
    assert after["data"]["page"]["markdown"].startswith("# Thank you for your order")


def test_cli(tmp_path: Path, test_site: SiteServer) -> None:
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(SCHEMA), encoding="utf-8")
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(FORM_STEPS), encoding="utf-8")

    one = invoke("structure", test_site.url("/article.html"), "--schema", str(schema_file))
    assert one.exit_code == 0, one.output
    data = json.loads(one.stdout)
    validate(data, SCHEMA)
    assert data == EXTRACTED

    several = invoke(
        "structure",
        test_site.url("/article.html"),
        test_site.url("/docs/"),
        "--schema",
        str(schema_file),
    )
    assert several.exit_code == 0, several.output
    lines = [json.loads(line) for line in several.stdout.splitlines()]
    assert [line["page"]["structured"] for line in lines] == [EXTRACTED, EXTRACTED]

    form = invoke("fetch", test_site.url("/form.html"), "--steps", str(steps_file))
    assert form.exit_code == 0, form.output
    assert form.stdout.startswith("# Thank you for your order")

    # With a goal the LLM ranks links; this fake cannot, so harvest goes on breadth first.
    goal = invoke(
        "harvest", test_site.url("/"), "--goal", "sourdough", "--max-pages", "2", "--json"
    )
    assert goal.exit_code == 0, goal.output
    assert len(goal.stdout.splitlines()) == 2


async def test_mcp(test_site: SiteServer) -> None:
    async with Client(create_server(Settings())) as client:
        extracted = await client.call_tool(
            "pdc_structure", {"urls": [test_site.url("/article.html")], "schema": SCHEMA}
        )
        opened = await client.call_tool(
            "pdc_session", {"action": "open", "url": test_site.url("/form.html")}
        )
        session_id = cast(dict[str, Any], opened.structured_content)["data"]["id"]
        after = await client.call_tool(
            "pdc_session", {"action": "step", "session_id": session_id, "steps": FORM_STEPS}
        )
        await client.call_tool("pdc_session", {"action": "close", "session_id": session_id})
        fetched = await client.call_tool(
            "pdc_fetch",
            {
                "url": test_site.url("/article.html"),
                "outputs": ["structured"],
                "structure": {"schema": SCHEMA, "instruction": "heading and days"},
            },
        )

    def data(result: Any) -> dict[str, Any]:
        envelope = cast(dict[str, Any], result.structured_content)
        assert envelope["error"] is None, envelope["error"]
        return cast(dict[str, Any], envelope["data"])

    validate(data(extracted)["data"], SCHEMA)
    assert data(after)["page"]["markdown"].startswith("# Thank you for your order")
    assert data(fetched)["structured"] == EXTRACTED


async def test_same_extraction_everywhere(tmp_path: Path, test_site: SiteServer) -> None:
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps(SCHEMA), encoding="utf-8")
    url = test_site.url("/article.html")
    with TestClient(create_app(Settings())) as client:
        via_rest = client.post(
            "/v1/structure", json={"urls": [url], "structure": {"schema": SCHEMA}}
        ).json()["data"]["data"]
    async with Client(create_server(Settings())) as client:
        result = await client.call_tool("pdc_structure", {"urls": [url], "schema": SCHEMA})
        via_mcp = cast(dict[str, Any], result.structured_content)["data"]["data"]
    cli_result = await asyncio.to_thread(invoke, "structure", url, "--schema", str(schema_file))
    via_cli = json.loads(cli_result.stdout)
    assert via_rest == via_mcp == via_cli == EXTRACTED
