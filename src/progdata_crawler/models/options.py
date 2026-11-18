"""Options shared by every operation that fetches a page.

The same names are used everywhere, so options that work for a single fetch also
work per page in harvest and bulk jobs.
"""

import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from progdata_crawler.models.steps import MAX_STEPS, Step

DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
"""Default maximum age of a cached result: 24 hours."""

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 120_000
MAX_WAIT_MS = 30_000


class Output(StrEnum):
    """A format the caller wants back for each page."""

    MARKDOWN = "markdown"
    HTML = "html"
    """Cleaned HTML."""
    RAW_HTML = "raw_html"
    LINKS = "links"
    IMAGES = "images"
    METADATA = "metadata"
    SCREENSHOT = "screenshot"
    SUMMARY = "summary"
    """Summary written by an LLM."""
    STRUCTURED = "structured"
    """Data extracted by an LLM against a schema."""
    DIFF = "diff"
    """Changes since the previous fetch of the same URL."""


DEFAULT_OUTPUTS: tuple[Output, ...] = (Output.MARKDOWN, Output.METADATA)


class ContentMode(StrEnum):
    """How much of the page to keep."""

    MAIN = "main"
    """Only the main content, without navigation, ads and footers."""
    FULL = "full"
    """The whole page."""


class Device(StrEnum):
    """Viewport and User-Agent variant."""

    DESKTOP = "desktop"
    MOBILE = "mobile"


class RenderMode(StrEnum):
    """When to load the page in a browser."""

    AUTO = "auto"
    """Use a browser only when the page needs JavaScript or browser features."""
    ALWAYS = "always"
    NEVER = "never"


class Scope(StrEnum):
    """How far discover and harvest may go from the start URL."""

    SITE = "site"
    """The same host only (a leading `www.` is ignored)."""
    SUBDOMAINS = "subdomains"
    """The host and its subdomains."""
    EXTERNAL = "external"
    """Any host."""


class Retention(StrEnum):
    """What may be stored after the response is sent."""

    DEFAULT = "default"
    """Store according to the normal retention periods."""
    NONE = "none"
    """Store no content after the response is sent."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


NonEmptyStr = Annotated[str, Field(min_length=1)]


class SelectorFilter(_StrictModel):
    """CSS selectors for elements to keep or remove."""

    include: tuple[NonEmptyStr, ...] = ()
    """Keep only these elements. Empty means keep everything."""
    exclude: tuple[NonEmptyStr, ...] = ()
    """Remove these elements."""


class PageOptions(_StrictModel):
    """Options for fetching and extracting a single page."""

    outputs: tuple[Output, ...] = Field(default=DEFAULT_OUTPUTS, min_length=1)
    content: ContentMode = ContentMode.MAIN
    tags: SelectorFilter = SelectorFilter()
    wait_ms: int = Field(default=0, ge=0, le=MAX_WAIT_MS)
    """Extra wait after the page has loaded, before extraction."""
    device: Device = Device.DESKTOP
    locale: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$",
        examples=["nb-NO", "en"],
    )
    """Preferred language, sent as Accept-Language."""
    geo: str | None = Field(default=None, pattern=r"^[A-Z]{2}$", examples=["NO"])
    """Preferred region as an ISO 3166-1 alpha-2 country code."""
    render: RenderMode = RenderMode.AUTO
    full_page_screenshot: bool = False
    """With the `screenshot` output: capture the whole page instead of the visible part."""
    cache_ttl: int = Field(default=DEFAULT_CACHE_TTL_SECONDS, ge=0)
    """Maximum age in seconds of a cached result. 0 always fetches a fresh copy."""
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, gt=0, le=MAX_TIMEOUT_MS)
    """Time limit for the page."""
    retain: Retention = Retention.DEFAULT
    steps: tuple[Step, ...] = Field(default=(), max_length=MAX_STEPS)
    """Browser steps to run after the page has loaded (click, type, press, scroll, wait,
    capture). Pages with steps always use the browser and are never cached."""

    @field_validator("outputs")
    @classmethod
    def _outputs_are_unique(cls, outputs: tuple[Output, ...]) -> tuple[Output, ...]:
        if len(set(outputs)) != len(outputs):
            raise ValueError("outputs must not contain duplicates")
        return outputs


class PathFilter(_StrictModel):
    """Regular expressions matched against URL paths."""

    include: tuple[NonEmptyStr, ...] = ()
    """Follow only paths that match one of these. Empty means all paths."""
    exclude: tuple[NonEmptyStr, ...] = ()
    """Never follow paths that match one of these."""

    @field_validator("include", "exclude")
    @classmethod
    def _patterns_compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        return patterns
