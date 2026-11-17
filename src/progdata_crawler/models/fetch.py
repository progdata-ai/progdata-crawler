"""Request model for fetching a single page."""

from pydantic import HttpUrl

from progdata_crawler.models.options import PageOptions


class FetchRequest(PageOptions):
    """Fetch one page and return the requested outputs."""

    url: HttpUrl
