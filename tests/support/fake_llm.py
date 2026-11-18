"""A fake LLM for tests: pydantic-ai's FunctionModel behind our Llm interface."""

from collections.abc import Callable

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from progdata_crawler.core.structure.llm import Llm


class FakeLlm:
    """Answers with `reply(prompt)` and remembers every prompt it was given."""

    def __init__(self, reply: Callable[[str], str] = lambda prompt: "A short summary.") -> None:
        self.prompts: list[str] = []
        self._reply = reply

    def _answer(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = str(messages[-1].parts[-1].content)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
        self.prompts.append(prompt)
        return ModelResponse(parts=[TextPart(self._reply(prompt))])

    def llm(self) -> Llm:
        return Llm(FunctionModel(self._answer), provider="fake", endpoint=None, robots=None)
