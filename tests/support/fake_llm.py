"""A fake LLM for tests: pydantic-ai's FunctionModel behind our Llm interface."""

from collections.abc import Callable
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from progdata_crawler.core.structure.llm import Llm

Reply = Callable[[str], str | dict[str, Any]]


class FakeLlm:
    """Answers with `reply(prompt)` and remembers every prompt it was given. A dict
    reply is sent as structured output, a string as text."""

    def __init__(self, reply: Reply = lambda prompt: "A short summary.") -> None:
        self.prompts: list[str] = []
        self.reply = reply

    def _answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = str(messages[-1].parts[-1].content)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
        self.prompts.append(prompt)
        answer = self.reply(prompt)
        if isinstance(answer, dict):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])
        return ModelResponse(parts=[TextPart(answer)])

    def llm(self) -> Llm:
        return Llm(FunctionModel(self._answer), provider="fake", endpoint=None, robots=None)
