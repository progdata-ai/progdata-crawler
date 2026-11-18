from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.store.cache import PageCache, cache_key
from progdata_crawler.core.store.sqlite import PageStore, fts_query
from progdata_crawler.models import ContentMode, Output, Page, PageMetadata, PageOptions, Retention
from progdata_crawler.models.options import SelectorFilter

URL = "https://example.com/bread"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def store(clock: Clock) -> AsyncIterator[PageStore]:
    async with PageStore(":memory:", clock=clock) as page_store:
        yield page_store


def page(
    markdown: str = "# Sourdough\n\nRye starter and wheat flour.", title: str = "Sourdough"
) -> Page:
    return Page(url=URL, markdown=markdown, metadata=PageMetadata(title=title, status=200))


async def test_put_and_get(store: PageStore, clock: Clock) -> None:
    await store.put("key", page(), rendered=True)
    cached = await store.get("key", max_age_s=60)
    assert cached is not None
    assert cached.page == page()
    assert cached.rendered is True
    assert cached.fetched_at == clock.now


async def test_get_respects_max_age(store: PageStore, clock: Clock) -> None:
    await store.put("key", page(), rendered=False)
    clock.now += 100
    assert await store.get("key", max_age_s=100) is not None
    assert await store.get("key", max_age_s=99) is None
    assert await store.get("other", max_age_s=1000) is None


async def test_put_replaces(store: PageStore) -> None:
    await store.put("key", page("old"), rendered=False)
    await store.put("key", page("new"), rendered=False)
    cached = await store.get("key", max_age_s=60)
    assert cached is not None and cached.page.markdown == "new"


async def test_full_text_search(store: PageStore) -> None:
    await store.put(
        "a", page(), rendered=False, title="Sourdough", body="Rye starter and wheat flour."
    )
    other = Page(url="https://example.com/buns", markdown="Cinnamon buns")
    await store.put("b", other, rendered=False, title="Buns", body="Cinnamon buns with cardamom.")

    hits = await store.search("wheat flour")
    assert [hit.url for hit in hits] == [URL]
    assert "[wheat]" in hits[0].snippet and "[flour]" in hits[0].snippet
    assert hits[0].title == "Sourdough"

    assert [hit.url for hit in await store.search("CARDAMOM")] == ["https://example.com/buns"]
    assert await store.search("nothing matches this") == []
    assert await store.search("   ") == []


async def test_search_ignores_diacritics_and_operators(store: PageStore) -> None:
    await store.put("a", page(), rendered=False, title="Kafé", body='Blåbær "syltetøy" AND NOT (x)')
    assert len(await store.search("blabaer")) == 0  # æ is a letter of its own, not a diacritic
    assert len(await store.search("kafe")) == 1
    assert len(await store.search('syltetøy" NOT "x')) == 1  # quotes cannot break out
    assert len(await store.search("NOT")) == 1  # a word, not an operator


async def test_reindexing_replaces_text(store: PageStore) -> None:
    await store.put("a", page(), rendered=False, body="first version")
    await store.put("b", page(), rendered=False, body="second version")
    assert await store.search("first") == []
    assert len(await store.search("second")) == 1


async def test_delete_older_than(store: PageStore, clock: Clock) -> None:
    await store.put("old", page(), rendered=False, body="old text")
    clock.now += 1000
    await store.put("new", Page(url="https://example.com/new"), rendered=False, body="new text")
    assert await store.delete_older_than(500) == 1
    assert await store.get("old", max_age_s=10_000) is None
    assert await store.get("new", max_age_s=10_000) is not None
    assert await store.search("old") == []


async def test_file_store_creates_directory_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cache.sqlite3"
    async with PageStore(path) as first:
        await first.put("key", page(), rendered=False, body="persisted text")
    async with PageStore(path) as second:
        assert await second.get("key", max_age_s=60) is not None
        assert len(await second.search("persisted")) == 1


def test_fts_query_quotes_every_word() -> None:
    assert fts_query('wheat "flour') == '"wheat" """flour"'
    assert fts_query("") == ""


def test_cache_key_covers_result_options_only() -> None:
    base = PageOptions()
    key = cache_key(URL, base)
    assert key == cache_key(URL, PageOptions(timeout_ms=5_000, cache_ttl=0, retain=Retention.NONE))
    assert key == cache_key(URL, PageOptions(outputs=(Output.METADATA, Output.MARKDOWN)))
    assert key != cache_key(URL + "?page=2", base)
    assert key != cache_key(URL, PageOptions(content=ContentMode.FULL))
    assert key != cache_key(URL, PageOptions(tags=SelectorFilter(exclude=("nav",))))
    assert key != cache_key(URL, PageOptions(locale="nb-NO"))
    assert key != cache_key(URL, PageOptions(outputs=(Output.MARKDOWN,)))


async def test_page_cache(store: PageStore, clock: Clock) -> None:
    cache = PageCache(store)
    options = PageOptions(cache_ttl=3600)
    await cache.put(URL, options, page(), rendered=False)

    hit = await cache.get(URL, options)
    assert hit is not None and hit.page == page()
    assert await cache.get(URL, PageOptions(cache_ttl=0)) is None

    clock.now += 3601
    assert await cache.get(URL, options) is None
    assert len(await store.search("starter")) == 1  # indexed from the markdown


async def test_page_cache_stores_nothing_with_retain_none(store: PageStore) -> None:
    cache = PageCache(store)
    options = PageOptions(retain=Retention.NONE)
    await cache.put(URL, options, page(), rendered=False)
    assert await cache.get(URL, PageOptions()) is None
    assert await store.search("starter") == []
