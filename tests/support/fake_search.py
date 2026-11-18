"""A search provider for tests: answers with hits that the test chooses."""

from progdata_crawler.models.find import FindRequest, SearchHit, SearchSource


class FakeSearch:
    name = "fake"
    sources = frozenset(SearchSource)

    def __init__(self, hits: dict[SearchSource, list[SearchHit]] | None = None) -> None:
        self.hits = hits or {}
        self.requests: list[tuple[FindRequest, SearchSource]] = []
        self.closed = False

    async def search(self, request: FindRequest, source: SearchSource) -> list[SearchHit]:
        self.requests.append((request, source))
        return self.hits.get(source, [])[: request.limit]

    async def aclose(self) -> None:
        self.closed = True


def web_hits(*urls: str) -> list[SearchHit]:
    return [
        SearchHit(url=url, title=f"Hit {n}", snippet="…", source=SearchSource.WEB)
        for n, url in enumerate(urls, start=1)
    ]
