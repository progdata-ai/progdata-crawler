"""The interface every search provider implements."""

from typing import Protocol

from progdata_crawler.models.find import FindRequest, SearchHit, SearchSource


class SearchProvider(Protocol):
    name: str

    async def search(self, request: FindRequest, source: SearchSource) -> list[SearchHit]:
        """Up to `request.limit` hits for one source. Raises `CrawlerError`."""
        ...

    async def aclose(self) -> None: ...
