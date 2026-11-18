"""Harvest: crawl a site within limits, as a job."""

from pydantic import Field, HttpUrl

from progdata_crawler.models.options import PageOptions, PathFilter, Scope

MAX_HARVEST_PAGES = 10_000
MAX_HARVEST_DEPTH = 20
MAX_GOAL_BUDGET = 100


class HarvestRequest(PageOptions):
    """Crawl from a start page, breadth first. The page options apply to every page."""

    url: HttpUrl
    """The start page."""
    max_pages: int = Field(default=100, ge=1, le=MAX_HARVEST_PAGES)
    """Most pages to fetch, including pages that end in an error."""
    max_depth: int = Field(default=3, ge=0, le=MAX_HARVEST_DEPTH)
    """Most links to follow from the start page. 0 fetches only the start page."""
    paths: PathFilter = PathFilter()
    """Which paths to fetch and follow. The start page is always fetched."""
    scope: Scope = Scope.SITE
    sitemaps: bool = True
    """Also start from the URLs in the site's sitemaps, as if linked from the start page."""
    goal: str | None = Field(default=None, min_length=1, max_length=500)
    """What you are looking for, for example "pricing and plans". The LLM then ranks the
    links found so far and the most relevant pages are fetched first. Only link texts
    and URLs are sent to the LLM."""
    goal_budget: int = Field(default=10, ge=0, le=MAX_GOAL_BUDGET)
    """Most LLM calls for ranking links. When they are spent, the crawl goes on breadth
    first."""
    notify_url: HttpUrl | None = None
    """Receives a signed POST when the job starts and when it finishes."""
