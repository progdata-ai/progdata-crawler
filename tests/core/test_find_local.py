import time

from progdata_crawler.core.find import search
from progdata_crawler.core.find.local import LocalSearch
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models import FindRequest, Page, PageMetadata, SearchSource


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


async def store_page(
    store: PageStore, url: str, title: str, text: str, published: str | None = None
) -> None:
    page = Page(
        url=url, markdown=text, metadata=PageMetadata(title=title, published_time=published)
    )
    await store.put(f"key:{url}", page, rendered=False, title=title, body=text)


async def test_finds_stored_pages() -> None:
    async with PageStore(":memory:") as store:
        await store_page(
            store, "https://a.example/rye", "Rye bread", "Rye needs a long rest.", "2026-10-01"
        )
        await store_page(store, "https://a.example/tomato", "Tomatoes", "Tomatoes like the sun.")
        result = await search(LocalSearch(store), FindRequest(query="rye rest"))

    assert result.provider == "local"
    assert [hit.url for hit in result.hits] == ["https://a.example/rye"]
    hit = result.hits[0]
    assert (hit.title, hit.source, hit.published) == ("Rye bread", SearchSource.WEB, "2026-10-01")
    assert "[Rye]" in hit.snippet
    assert result.notes == ()


async def test_news_and_images_say_why_they_are_empty() -> None:
    async with PageStore(":memory:") as store:
        await store_page(store, "https://a.example/rye", "Rye", "Rye bread.")
        request = FindRequest.model_validate({"query": "rye", "sources": ["web", "news", "images"]})
        result = await search(LocalSearch(store), request)
    assert [hit.url for hit in result.hits] == ["https://a.example/rye"]
    assert result.notes == (
        "the local search provider has no news index",
        "the local search provider has no images index",
    )


async def test_time_range_filters_on_when_pages_were_fetched() -> None:
    clock = Clock()
    async with PageStore(":memory:", clock=clock) as store:
        clock.now = time.time() - 10 * 24 * 3600
        await store_page(store, "https://a.example/old", "Old", "Sourdough from last week.")
        clock.now = time.time()
        await store_page(store, "https://a.example/new", "New", "Sourdough from today.")
        provider = LocalSearch(store)
        week = await search(
            provider, FindRequest.model_validate({"query": "sourdough", "time_range": "week"})
        )
        year = await search(
            provider, FindRequest.model_validate({"query": "sourdough", "time_range": "year"})
        )
    assert [hit.url for hit in week.hits] == ["https://a.example/new"]
    assert {hit.url for hit in year.hits} == {"https://a.example/old", "https://a.example/new"}


async def test_limit_and_no_matches() -> None:
    async with PageStore(":memory:") as store:
        for n in range(5):
            await store_page(store, f"https://a.example/{n}", f"Bread {n}", "Bread and butter.")
        provider = LocalSearch(store)
        limited = await search(provider, FindRequest(query="bread", limit=2))
        nothing = await search(provider, FindRequest(query="zebra"))
    assert len(limited.hits) == 2
    assert nothing.hits == ()
