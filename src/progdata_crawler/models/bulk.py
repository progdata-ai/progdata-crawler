"""Bulk: fetch many URLs in one job."""

from pydantic import Field, HttpUrl

from progdata_crawler.models.options import PageOptions

MAX_BULK_URLS = 10_000


class BulkRequest(PageOptions):
    """Fetch every URL with the same page options. Duplicates are fetched once."""

    urls: tuple[HttpUrl, ...] = Field(min_length=1, max_length=MAX_BULK_URLS)
    notify_url: HttpUrl | None = None
    """Receives a signed POST when the job starts and when it finishes."""
