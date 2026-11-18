"""Discover: find the URLs of a site without fetching every page."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from progdata_crawler.models.options import PathFilter, Retention, Scope

MAX_DISCOVER_LIMIT = 50_000


class DiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    """The site to discover: its start page."""
    query: str | None = Field(default=None, max_length=500)
    """Keep only URLs whose address or link text contains every word."""
    paths: PathFilter = PathFilter()
    scope: Scope = Scope.SITE
    sitemaps: bool = True
    """Read sitemap.xml files (from robots.txt, or /sitemap.xml)."""
    links: bool = True
    """Read the links on the start page."""
    limit: int = Field(default=1_000, ge=1, le=MAX_DISCOVER_LIMIT)
    retain: Retention = Retention.DEFAULT
    """`none` stores nothing: the start page read for its links is not cached."""


class UrlSource(StrEnum):
    SITEMAP = "sitemap"
    LINK = "link"


class DiscoveredUrl(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    """Normalized URL."""
    source: UrlSource
    text: str = ""
    """Link text, for URLs found as links."""
    lastmod: str | None = None
    """From the sitemap, as the site states it."""


class DiscoverResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    urls: tuple[DiscoveredUrl, ...]
    sitemaps: tuple[str, ...] = ()
    """The sitemap files that were read."""
    truncated: bool = False
    """True when more URLs were found than `limit` allowed."""
