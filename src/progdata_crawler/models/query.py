"""Query: search the content of pages fetched before."""

from pydantic import BaseModel, ConfigDict, Field

MAX_QUERY_HITS = 100


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=500)
    """Words to look for. Every word must match; there are no operators."""
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_HITS)


class QueryHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    title: str
    snippet: str
    """A short excerpt with the matching words in [brackets]."""
    fetched_at: float
    """Unix time the page was fetched."""


class QueryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    hits: tuple[QueryHit, ...]
    """Best matches first."""
