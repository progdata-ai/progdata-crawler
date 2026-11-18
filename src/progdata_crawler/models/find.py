"""Find: web search through a search provider."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_FIND_RESULTS = 20


class SearchSource(StrEnum):
    WEB = "web"
    NEWS = "news"
    IMAGES = "images"


class TimeRange(StrEnum):
    """Only results from the last day, week, month or year."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class FindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=400)
    sources: tuple[SearchSource, ...] = Field(default=(SearchSource.WEB,), min_length=1)
    time_range: TimeRange | None = None
    limit: int = Field(default=10, ge=1, le=MAX_FIND_RESULTS)
    """Most results per source."""
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$", examples=["NO"])
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$", examples=["nb"])
    """Language of the results, as an ISO 639-1 code."""


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    title: str
    snippet: str = ""
    """Plain text, without markup."""
    source: SearchSource
    published: str | None = None
    """When the page was published, as the provider states it."""
    image_url: str | None = None
    """For image results: the image itself. `url` is the page it is on."""


class FindResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    provider: str
    hits: tuple[SearchHit, ...]
