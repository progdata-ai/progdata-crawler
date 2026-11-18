"""Search the content of pages fetched before, with the full-text index."""

from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models.query import QueryHit, QueryRequest, QueryResult


async def query_pages(store: PageStore, request: QueryRequest) -> QueryResult:
    """Best matches first. Only pages stored with the default retention are indexed."""
    hits = await store.search(request.query, limit=request.limit)
    return QueryResult(
        query=request.query,
        hits=tuple(
            QueryHit(url=hit.url, title=hit.title, snippet=hit.snippet, fetched_at=hit.fetched_at)
            for hit in hits
        ),
    )
