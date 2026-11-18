import pytest

from progdata_crawler.core.store.query import query_pages
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models import Page, QueryRequest


async def test_query_returns_url_title_and_snippet() -> None:
    async with PageStore(":memory:") as store:
        for i, text in enumerate(["Rye bread needs a long rest.", "Tomatoes like the sun."]):
            page = Page(url=f"https://example.test/{i}", markdown=text)
            await store.put(f"key{i}", page, rendered=False, title=f"Page {i}", body=text)
        result = await query_pages(store, QueryRequest(query="rye rest"))
    assert result.query == "rye rest"
    assert [(hit.url, hit.title) for hit in result.hits] == [("https://example.test/0", "Page 0")]
    assert "[Rye]" in result.hits[0].snippet
    assert result.hits[0].fetched_at > 0


def test_limits() -> None:
    with pytest.raises(ValueError):
        QueryRequest(query="")
    with pytest.raises(ValueError):
        QueryRequest(query="x", limit=101)
