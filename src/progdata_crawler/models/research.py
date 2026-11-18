"""Research: answer a question from the web, with sources."""

from pydantic import BaseModel, ConfigDict, Field


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=3, max_length=2_000)
    max_rounds: int = Field(default=2, ge=1, le=4)
    """Rounds of searching; a later round looks for what the answer still lacks."""
    max_searches: int = Field(default=6, ge=1, le=30)
    max_pages: int = Field(default=8, ge=1, le=40)
    """Most pages fetched and read."""
    max_seconds: int = Field(default=120, ge=10, le=600)
    """Searching and fetching stop after this; the answer is then written from what was read."""


class ResearchSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    """The number the answer cites, as [n]."""
    url: str
    title: str


class ResearchUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    searches: int = 0
    pages: int = 0
    llm_calls: int = 0
    rounds: int = 0


class ResearchAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    answer: str
    """Markdown, citing sources as [n]."""
    sources: tuple[ResearchSource, ...]
    """The cited sources. Every one was fetched and read."""
    complete: bool
    """False when the answer says information is missing, for example because the budget
    ran out."""
    notes: tuple[str, ...] = ()
    usage: ResearchUsage = ResearchUsage()
