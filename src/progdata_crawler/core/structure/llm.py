"""The LLM layer: one small interface for every feature that asks a language model.

pydantic-ai does the work behind it, so the provider (Anthropic, Azure OpenAI, Ollama)
and even the library can change without touching the features that use it. Tests give
`Llm` a fake model and never call a real API.

Rules every call follows:

- Calls to hosted APIs are checked against the API host's robots.txt, like any other
  request. A local Ollama is the operator's own service and is not checked.
- The endpoint comes from the operator's settings, never from a request, so it may be an
  internal address (a private Azure endpoint, Ollama on the same machine).
- Prompts and page content are never logged; provider, model, tokens and time are.
- Input longer than `PDC_LLM_MAX_INPUT_CHARS` is cut before it is sent.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelAPIError,
    ModelRateLimitError,
    RunTimedOut,
    UnexpectedModelBehavior,
)
from pydantic_ai.models import Model

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.settings import LlmProviderName, Settings

log = logging.getLogger(__name__)

# The library prints a banner on its first run; our CLI and MCP output must stay clean.
pydantic_ai.BANNER_ENABLED = False

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
TRUNCATION_NOTE = "\n\n[The rest of the content was cut to fit the size limit.]"

RobotsCheck = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class LlmUsage:
    input_tokens: int
    output_tokens: int
    requests: int


@dataclass(frozen=True)
class LlmAnswer[T]:
    output: T
    usage: LlmUsage
    model: str
    truncated: bool
    """The input was cut to fit `max_input_chars`."""


class Llm:
    """Ask a model. Safe to share between concurrent requests."""

    def __init__(
        self,
        model: Model,
        *,
        provider: str,
        endpoint: str | None,
        robots: RobotsCheck | None,
        timeout_s: float = 60.0,
        max_input_chars: int = 100_000,
    ) -> None:
        """`endpoint` is the URL checked against robots.txt before each call; None for
        services the operator runs (Ollama) and for test models."""
        self._model = model
        self.provider = provider
        self._endpoint = endpoint
        self._robots = robots
        self._timeout_s = timeout_s
        self._max_input_chars = max_input_chars

    @property
    def model_name(self) -> str:
        return self._model.model_name

    async def run[T](
        self,
        *,
        instructions: str,
        prompt: str,
        output_type: type[T],
        retries: int = 1,
    ) -> LlmAnswer[T]:
        """Ask the model and return its answer as `output_type`. With a structured type,
        an answer that does not validate is sent back to the model up to `retries` times.
        Raises `CrawlerError` (`llm_unavailable`, `rate_limited`, `robots_disallowed`)."""
        if self._endpoint is not None and self._robots is not None:
            await self._robots(self._endpoint)
        truncated = len(prompt) > self._max_input_chars
        if truncated:
            prompt = prompt[: self._max_input_chars] + TRUNCATION_NOTE

        agent = Agent(self._model, output_type=output_type, instructions=instructions)
        started = time.monotonic()
        context = {"provider": self.provider, "model": self.model_name}
        try:
            result = await agent.run(prompt, retries=retries, timeout=self._timeout_s)
        except ModelRateLimitError as error:
            raise CrawlerError(
                ErrorCode.RATE_LIMITED, "the LLM provider's limit was reached", **context
            ) from error
        except RunTimedOut as error:
            raise CrawlerError(
                ErrorCode.LLM_UNAVAILABLE, "the LLM did not answer in time", **context
            ) from error
        except UnexpectedModelBehavior as error:
            raise CrawlerError(
                ErrorCode.LLM_UNAVAILABLE,
                "the LLM did not give a usable answer",
                **context,
            ) from error
        except (ModelAPIError, AgentRunError) as error:
            log.warning("llm call failed", extra={**context, "error": type(error).__name__})
            raise CrawlerError(
                ErrorCode.LLM_UNAVAILABLE,
                "the LLM provider refused or failed the request",
                **context,
            ) from error

        usage = result.usage
        answer_usage = LlmUsage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            requests=usage.requests,
        )
        log.info(
            "llm call",
            extra={
                **context,
                "input_tokens": answer_usage.input_tokens,
                "output_tokens": answer_usage.output_tokens,
                "requests": answer_usage.requests,
                "took_ms": round((time.monotonic() - started) * 1000),
                "truncated": truncated,
            },
        )
        return LlmAnswer(
            output=result.output, usage=answer_usage, model=self.model_name, truncated=truncated
        )


def _not_configured(setting: str) -> CrawlerError:
    return CrawlerError(ErrorCode.LLM_UNAVAILABLE, f"the LLM is not configured: set {setting}")


def create_llm(settings: Settings, *, robots: RobotsCheck) -> Llm:
    """The LLM `PDC_LLM_PROVIDER` names. Raises `llm_unavailable` when it is not set up.
    Nothing is sent until the first call."""

    def build(model: Model, provider: str, endpoint: str | None) -> Llm:
        return Llm(
            model,
            provider=provider,
            endpoint=endpoint,
            robots=robots,
            timeout_s=settings.llm_timeout_s,
            max_input_chars=settings.llm_max_input_chars,
        )

    match settings.llm_provider:
        case None:
            raise _not_configured("PDC_LLM_PROVIDER")
        case LlmProviderName.ANTHROPIC:
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            if settings.anthropic_api_key is None:
                raise _not_configured("PDC_ANTHROPIC_API_KEY")
            model = AnthropicModel(
                settings.llm_model or DEFAULT_ANTHROPIC_MODEL,
                provider=AnthropicProvider(api_key=settings.anthropic_api_key.get_secret_value()),
            )
            return build(model, "anthropic", ANTHROPIC_ENDPOINT)
        case LlmProviderName.AZURE_OPENAI:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.azure import AzureProvider

            missing = [
                name
                for name, value in (
                    ("PDC_AZURE_OPENAI_ENDPOINT", settings.azure_openai_endpoint),
                    ("PDC_AZURE_OPENAI_API_KEY", settings.azure_openai_api_key),
                    ("PDC_AZURE_OPENAI_API_VERSION", settings.azure_openai_api_version),
                    ("PDC_LLM_MODEL", settings.llm_model),
                )
                if value is None
            ]
            if missing:
                raise _not_configured(", ".join(missing))
            assert settings.azure_openai_endpoint is not None
            assert settings.azure_openai_api_key is not None
            assert settings.llm_model is not None
            endpoint = settings.azure_openai_endpoint.rstrip("/")
            model = OpenAIChatModel(
                settings.llm_model,
                provider=AzureProvider(
                    azure_endpoint=endpoint,
                    api_version=settings.azure_openai_api_version,
                    api_key=settings.azure_openai_api_key.get_secret_value(),
                ),
            )
            chat_url = f"{endpoint}/openai/deployments/{settings.llm_model}/chat/completions"
            return build(model, "azure_openai", chat_url)
        case LlmProviderName.OLLAMA:
            from pydantic_ai.models.ollama import OllamaModel
            from pydantic_ai.providers.ollama import OllamaProvider

            if settings.llm_model is None:
                raise _not_configured("PDC_LLM_MODEL")
            model = OllamaModel(
                settings.llm_model, provider=OllamaProvider(base_url=settings.ollama_url)
            )
            return build(model, "ollama", None)
