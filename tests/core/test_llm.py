import asyncio
import logging
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelAuthenticationError, ModelHTTPError, ModelRateLimitError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.structure.llm import ANTHROPIC_ENDPOINT, TRUNCATION_NOTE, Llm, create_llm
from progdata_crawler.models import ErrorCode
from progdata_crawler.settings import Settings

ENDPOINT = "https://llm.example/v1/chat"


class Robots:
    def __init__(self, refuse: bool = False) -> None:
        self.checked: list[str] = []
        self._refuse = refuse

    async def __call__(self, url: str) -> None:
        self.checked.append(url)
        if self._refuse:
            raise CrawlerError(ErrorCode.ROBOTS_DISALLOWED, "no", url=url)


def last_prompt(messages: list[ModelMessage]) -> str:
    return str(messages[-1].parts[-1].content)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]


def llm(model: FunctionModel | TestModel, robots: Robots | None = None, **options: float) -> Llm:
    return Llm(
        model,
        provider="fake",
        endpoint=ENDPOINT,
        robots=robots or Robots(),
        timeout_s=options.get("timeout_s", 5.0),
        max_input_chars=int(options.get("max_input_chars", 100_000)),
    )


async def test_text_answer_with_usage() -> None:
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"You said: {last_prompt(messages)}")])

    robots = Robots()
    result = await llm(FunctionModel(answer), robots).run(
        instructions="Repeat.", prompt="hello", output_type=str
    )
    assert result.output == "You said: hello"
    assert result.usage.requests == 1
    assert result.usage.input_tokens > 0
    assert not result.truncated
    assert robots.checked == [ENDPOINT]


async def test_structured_answer() -> None:
    class Price(BaseModel):
        amount: float
        currency: str

    model = TestModel(custom_output_args={"amount": 12.5, "currency": "NOK"})
    result = await llm(model).run(instructions="Extract.", prompt="12,50 kr", output_type=Price)
    assert result.output == Price(amount=12.5, currency="NOK")


async def test_long_input_is_cut() -> None:
    seen: list[str] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(last_prompt(messages))
        return ModelResponse(parts=[TextPart("ok")])

    result = await llm(FunctionModel(answer), max_input_chars=1_000).run(
        instructions="Read.", prompt="x" * 5_000, output_type=str
    )
    assert result.truncated
    assert seen[0] == "x" * 1_000 + TRUNCATION_NOTE


async def test_robots_txt_is_respected() -> None:
    called: list[bool] = []

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        called.append(True)
        return ModelResponse(parts=[TextPart("ok")])

    with pytest.raises(CrawlerError) as raised:
        await llm(FunctionModel(answer), Robots(refuse=True)).run(
            instructions="x", prompt="y", output_type=str
        )
    assert raised.value.code is ErrorCode.ROBOTS_DISALLOWED
    assert called == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ModelRateLimitError("fake", "slow down"), ErrorCode.RATE_LIMITED),
        (ModelAuthenticationError("fake", "bad key"), ErrorCode.LLM_UNAVAILABLE),
        (ModelHTTPError(500, "fake", {"error": "boom"}), ErrorCode.LLM_UNAVAILABLE),
    ],
)
async def test_provider_errors(error: Exception, code: ErrorCode) -> None:
    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise error

    with pytest.raises(CrawlerError) as raised:
        await llm(FunctionModel(fail)).run(instructions="x", prompt="y", output_type=str)
    assert raised.value.code is code
    assert raised.value.details["provider"] == "fake"


async def test_slow_model_times_out() -> None:
    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart("late")])

    with pytest.raises(CrawlerError) as raised:
        await llm(FunctionModel(slow), timeout_s=0.2).run(
            instructions="x", prompt="y", output_type=str
        )
    assert raised.value.code is ErrorCode.LLM_UNAVAILABLE
    assert "in time" in raised.value.message


async def test_prompts_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("the secret answer")])

    with caplog.at_level(logging.DEBUG):
        await llm(FunctionModel(answer)).run(
            instructions="x", prompt="a very private page", output_type=str
        )
    records = [r for r in caplog.records if r.name.startswith("progdata_crawler")]
    assert records, "the call is logged"
    call = records[-1]
    assert getattr(call, "input_tokens", 0) > 0
    for record in caplog.records:
        text = record.getMessage() + str(record.__dict__)
        assert "private page" not in text and "secret answer" not in text


async def robots_ok(url: str) -> None:
    return None


def test_create_from_settings(tmp_path: Path) -> None:
    anthropic = create_llm(
        Settings(data_dir=tmp_path, llm_provider="anthropic", anthropic_api_key="k"),  # pyright: ignore[reportArgumentType]
        robots=robots_ok,
    )
    assert (anthropic.provider, anthropic.model_name) == ("anthropic", "claude-sonnet-5-5")
    assert anthropic._endpoint == ANTHROPIC_ENDPOINT  # pyright: ignore[reportPrivateUsage]

    ollama = create_llm(
        Settings(data_dir=tmp_path, llm_provider="ollama", llm_model="small"),  # pyright: ignore[reportArgumentType]
        robots=robots_ok,
    )
    assert (ollama.provider, ollama.model_name) == ("ollama", "small")
    assert ollama._endpoint is None  # pyright: ignore[reportPrivateUsage]

    azure = create_llm(
        Settings(
            data_dir=tmp_path,
            llm_provider="azure_openai",  # pyright: ignore[reportArgumentType]
            llm_model="gpt-deploy",
            azure_openai_endpoint="https://res.openai.azure.com/",
            azure_openai_api_key="k",  # pyright: ignore[reportArgumentType]
            azure_openai_api_version="2026-01-01",
        ),
        robots=robots_ok,
    )
    assert azure._endpoint == (  # pyright: ignore[reportPrivateUsage]
        "https://res.openai.azure.com/openai/deployments/gpt-deploy/chat/completions"
    )


@pytest.mark.parametrize(
    ("fields", "missing"),
    [
        ({}, "PDC_LLM_PROVIDER"),
        ({"llm_provider": "anthropic"}, "PDC_ANTHROPIC_API_KEY"),
        ({"llm_provider": "ollama"}, "PDC_LLM_MODEL"),
        ({"llm_provider": "azure_openai", "llm_model": "d"}, "PDC_AZURE_OPENAI_ENDPOINT"),
    ],
)
def test_missing_configuration(tmp_path: Path, fields: dict[str, str], missing: str) -> None:
    with pytest.raises(CrawlerError) as raised:
        create_llm(Settings.model_validate({"data_dir": tmp_path, **fields}), robots=robots_ok)
    assert raised.value.code is ErrorCode.LLM_UNAVAILABLE
    assert missing in raised.value.message
