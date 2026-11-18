"""Local search: find over the pages we have already fetched.

The provider searches the full-text index of stored pages (FTS5). It sends no requests,
needs no API key and works offline. Only pages that were fetched, and are still within
the retention period, can be found. The time range filters on when a page was fetched,
since that is the date we know for every page.
"""

import time

from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models.find import FindRequest, SearchHit, SearchSource, TimeRange

_RANGE_S = {
    TimeRange.DAY: 24 * 3600,
    TimeRange.WEEK: 7 * 24 * 3600,
    TimeRange.MONTH: 31 * 24 * 3600,
    TimeRange.YEAR: 366 * 24 * 3600,
}


class LocalSearch:
    name = "local"
    sources = frozenset({SearchSource.WEB})

    def __init__(self, store: PageStore) -> None:
        self._store = store

    async def search(self, request: FindRequest, source: SearchSource) -> list[SearchHit]:
        if source not in self.sources:
            return []
        since = (
            time.time() - _RANGE_S[request.time_range] if request.time_range is not None else None
        )
        found = await self._store.search(request.query, limit=request.limit, since=since)
        hits: list[SearchHit] = []
        for item in found:
            page = await self._store.latest_page(item.url)
            metadata = page.metadata if page is not None else None
            hits.append(
                SearchHit(
                    url=item.url,
                    title=item.title or item.url,
                    snippet=item.snippet,
                    source=SearchSource.WEB,
                    published=metadata.published_time if metadata is not None else None,
                )
            )
        return hits

    async def aclose(self) -> None:
        """The store belongs to the crawler, which closes it."""
