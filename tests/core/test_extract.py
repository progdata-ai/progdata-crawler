from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.structure.extract import extract, schema_problems
from progdata_crawler.models import ErrorCode, FetchRequest
from progdata_crawler.models.structure import StructureSpec
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.site_server import SiteServer

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "days": {"type": "integer", "minimum": 1},
    },
    "required": ["title", "days"],
    "additionalProperties": False,
}
SPEC = StructureSpec.model_validate({"schema": SCHEMA, "instruction": "the recipe's name and days"})


async def test_valid_answer_is_returned() -> None:
    fake = FakeLlm(lambda prompt: {"title": "Sourdough", "days": 3})
    result = await extract(fake.llm(), "# Sourdough\n\nIt takes three days.", SPEC, title="Bread")
    assert result == {"title": "Sourdough", "days": 3}
    assert fake.prompts[0].startswith("Title: Bread")


async def test_invalid_answer_is_retried_with_the_problems() -> None:
    answers: list[dict[str, Any]] = [
        {"title": "Sourdough", "days": "three"},
        {"title": "Sourdough", "days": 3},
    ]
    fake = FakeLlm(lambda prompt: answers.pop(0))
    result = await extract(fake.llm(), "page", SPEC)
    assert result == {"title": "Sourdough", "days": 3}
    assert len(fake.prompts) == 2
    assert "days: 'three' is not of type 'integer'" in fake.prompts[1]


async def test_gives_up_with_a_clear_error() -> None:
    fake = FakeLlm(lambda prompt: {"title": "Sourdough"})
    with pytest.raises(CrawlerError) as raised:
        await extract(fake.llm(), "page", SPEC, retries=1)
    assert raised.value.code is ErrorCode.EXTRACTION_FAILED
    assert "after 2 tries" in raised.value.message
    assert "'days' is a required property" in raised.value.message
    assert len(fake.prompts) == 2


def test_schema_problems() -> None:
    assert schema_problems(SCHEMA, {"title": "x", "days": 2}) == []
    problems = schema_problems(SCHEMA, {"title": 1, "days": 0, "extra": True})
    assert any(p.startswith("days:") for p in problems)
    assert any(p.startswith("title:") for p in problems)
    assert any("extra" in p for p in problems)


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "array"}, "top level"),
        ({"type": "object", "properties": {"x": {"type": "nonsense"}}}, "not a valid JSON Schema"),
        ({"type": "object", "x": "y" * 40_000}, "larger than"),
    ],
)
def test_unusable_schemas_are_refused(schema: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        StructureSpec.model_validate({"schema": schema})


def test_deeply_nested_schemas_are_refused() -> None:
    schema: dict[str, Any] = {"type": "object"}
    inner = schema
    for _ in range(10):
        inner["properties"] = {"x": {"type": "object"}}
        inner = inner["properties"]["x"]
    with pytest.raises(ValueError, match="nested deeper"):
        StructureSpec.model_validate({"schema": schema})


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    fake = FakeLlm(lambda prompt: {"title": "How we bake our sourdough", "days": 3})
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with Crawler(settings, store_path=":memory:", llm=fake.llm()) as instance:
        yield instance


async def test_structured_output_in_fetch(crawler: Crawler, test_site: SiteServer) -> None:
    request = FetchRequest.model_validate(
        {
            "url": test_site.url("/article.html"),
            "outputs": ["structured", "metadata"],
            "structure": {"schema": SCHEMA, "instruction": "name and days"},
        }
    )
    envelope = await crawler.fetch_envelope(request)
    assert envelope.data is not None, envelope.error
    assert envelope.data.structured == {"title": "How we bake our sourdough", "days": 3}
    dumped = envelope.model_dump(mode="json")
    assert dumped["data"]["structured"]["days"] == 3


async def test_structured_needs_a_structure(crawler: Crawler, test_site: SiteServer) -> None:
    request = FetchRequest.model_validate(
        {"url": test_site.url("/article.html"), "outputs": ["structured"]}
    )
    envelope = await crawler.fetch_envelope(request)
    assert envelope.error is not None and envelope.error.code is ErrorCode.INVALID_REQUEST


async def test_failed_extraction_keeps_the_page(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    fake = FakeLlm(lambda prompt: {"wrong": True})
    async with Crawler(
        Settings(data_dir=tmp_path), store_path=":memory:", llm=fake.llm()
    ) as crawler:
        request = FetchRequest.model_validate(
            {
                "url": test_site.url("/article.html"),
                "outputs": ["markdown", "structured"],
                "structure": {"schema": SCHEMA},
            }
        )
        envelope = await crawler.fetch_envelope(request)
    assert envelope.data is not None and envelope.data.markdown is not None
    assert envelope.data.structured is None
    assert envelope.data.output_errors is not None
    assert envelope.data.output_errors["structured"].code is ErrorCode.EXTRACTION_FAILED


def test_the_schema_is_called_schema_in_requests() -> None:
    request = FetchRequest.model_validate(
        {"url": "https://example.test/", "structure": {"schema": SCHEMA}}
    )
    assert request.model_dump(mode="json")["structure"]["schema"] == SCHEMA
    assert "schema" in FetchRequest.model_json_schema()["$defs"]["StructureSpec"]["properties"]
