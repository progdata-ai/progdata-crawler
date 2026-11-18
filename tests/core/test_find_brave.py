import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.find import create_provider, search
from progdata_crawler.core.find.brave import BraveSearch, plain_text
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models import ErrorCode, FindRequest, SearchSource
from progdata_crawler.settings import Settings, default_user_agent

RECORDED = Path(__file__).parent.parent / "support" / "search"
KEY = "test-key"


def recorded(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / f"brave_{name}.json").read_text(encoding="utf-8"))


class Recorder:
    """A fake Brave API that answers from the recorded files and remembers requests."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._respond is not None:
            return self._respond(request)
        source = request.url.path.rsplit("/", 2)[-2]  # .../web/search -> web
        return httpx.Response(200, json=recorded(source))


class Robots:
    """Stands in for `Crawler.check_robots`: remembers URLs, refuses those it is told to."""

    def __init__(self, disallow: str | None = None) -> None:
        self.checked: list[str] = []
        self._disallow = disallow

    async def __call__(self, url: str) -> None:
        self.checked.append(url)
        if self._disallow is not None and self._disallow in url:
            raise CrawlerError(ErrorCode.ROBOTS_DISALLOWED, "no", url=url)


def brave(recorder: Recorder, key: str | None = KEY, robots: Robots | None = None) -> BraveSearch:
    return BraveSearch(
        key,
        user_agent=default_user_agent(),
        guard=AddressGuard(allow_private=False),
        robots=robots or Robots(),
        transport=httpx.MockTransport(recorder),
    )


def request(**fields: Any) -> FindRequest:
    return FindRequest.model_validate({"query": "sourdough starter", **fields})


async def test_web_results() -> None:
    recorder = Recorder()
    provider = brave(recorder)
    result = await search(provider, request())
    await provider.aclose()

    assert result.provider == "brave"
    assert [hit.url for hit in result.hits] == [
        "https://bakery.example/starter",
        "https://flour.example/rye",
    ]
    first = result.hits[0]
    assert first.title == "How to keep a sourdough starter alive"
    assert first.snippet == "Feed it flour & water every day. A starter likes warmth."
    assert first.published == "2026-10-02T08:00:00"
    assert first.source is SearchSource.WEB

    sent = recorder.requests[0]
    assert sent.url.path == "/res/v1/web/search"
    assert sent.url.params["q"] == "sourdough starter"
    assert sent.url.params["count"] == "10"
    assert sent.headers["X-Subscription-Token"] == KEY
    assert sent.headers["User-Agent"].startswith("progdata-crawler/")


async def test_several_sources_with_filters() -> None:
    recorder = Recorder()
    provider = brave(recorder)
    result = await search(
        provider,
        request(
            sources=["web", "news", "images"],
            time_range="week",
            country="NO",
            language="nb",
            limit=5,
        ),
    )
    await provider.aclose()

    sources = [hit.source for hit in result.hits]
    assert sources == [SearchSource.WEB] * 2 + [SearchSource.NEWS] + [SearchSource.IMAGES] * 2
    news = result.hits[2]
    assert news.url == "https://news.example/sourdough-boom"
    assert news.published == "2026-11-16T10:00:00"
    images = result.hits[3:]
    assert [hit.image_url for hit in images] == [
        "https://img.bakery.example/jar.jpg",
        "https://img.bakery.example/bubbles.jpg",
    ]
    assert {r.url.path for r in recorder.requests} == {
        "/res/v1/web/search",
        "/res/v1/news/search",
        "/res/v1/images/search",
    }
    for sent in recorder.requests:
        assert sent.url.params["freshness"] == "pw"
        assert sent.url.params["country"] == "no"
        assert sent.url.params["search_lang"] == "nb"
        assert sent.url.params["count"] == "5"


async def test_limit_cuts_the_results() -> None:
    provider = brave(Recorder())
    result = await search(provider, request(limit=1))
    await provider.aclose()
    assert len(result.hits) == 1


def status(code: int, headers: dict[str, str] | None = None) -> Recorder:
    return Recorder(lambda _: httpx.Response(code, json={"error": "x"}, headers=headers))


@pytest.mark.parametrize(
    ("recorder", "code"),
    [
        (status(401), ErrorCode.SEARCH_UNAVAILABLE),
        (status(500), ErrorCode.SEARCH_UNAVAILABLE),
        (status(422), ErrorCode.INVALID_REQUEST),
        (
            Recorder(lambda _: httpx.Response(200, content=b"not json")),
            ErrorCode.SEARCH_UNAVAILABLE,
        ),
    ],
)
async def test_provider_errors(recorder: Recorder, code: ErrorCode) -> None:
    provider = brave(recorder)
    with pytest.raises(CrawlerError) as raised:
        await search(provider, request())
    await provider.aclose()
    assert raised.value.code is code
    assert KEY not in str(raised.value.details)


async def test_rate_limited_says_when_to_retry() -> None:
    provider = brave(status(429, {"X-RateLimit-Reset": "3, 86000"}))
    with pytest.raises(CrawlerError) as raised:
        await search(provider, request())
    await provider.aclose()
    assert raised.value.code is ErrorCode.RATE_LIMITED
    assert raised.value.details["retry_after_s"] == 3


async def test_network_errors() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for respond, code in ((refuse, ErrorCode.SEARCH_UNAVAILABLE), (slow, ErrorCode.FETCH_TIMEOUT)):
        provider = brave(Recorder(respond))
        with pytest.raises(CrawlerError) as raised:
            await search(provider, request())
        await provider.aclose()
        assert raised.value.code is code


async def test_without_a_key_nothing_is_sent() -> None:
    recorder = Recorder()
    provider = brave(recorder, key=None)
    with pytest.raises(CrawlerError) as raised:
        await search(provider, request())
    await provider.aclose()
    assert raised.value.code is ErrorCode.SEARCH_UNAVAILABLE
    assert "PDC_BRAVE_API_KEY" in raised.value.message
    assert recorder.requests == []


async def test_provider_comes_from_settings(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, brave_api_key="k")  # pyright: ignore[reportArgumentType]
    async with PageStore(":memory:") as store:
        provider = create_provider(settings, robots=Robots(), store=store)
        assert provider.name == "brave"
        await provider.aclose()


def test_plain_text() -> None:
    assert plain_text("a <strong>b</strong> &amp; c") == "a b & c"
    assert plain_text(None) == ""


def test_request_limits() -> None:
    with pytest.raises(ValueError):
        FindRequest(query="")
    with pytest.raises(ValueError):
        FindRequest.model_validate({"query": "x", "limit": 21})
    with pytest.raises(ValueError):
        FindRequest.model_validate({"query": "x", "country": "norway"})
    with pytest.raises(ValueError):
        FindRequest.model_validate({"query": "x", "sources": []})


async def test_every_call_is_checked_against_robots_txt() -> None:
    robots = Robots()
    recorder = Recorder()
    provider = brave(recorder, robots=robots)
    await search(provider, request(sources=["web", "news"]))
    await provider.aclose()
    assert sorted(robots.checked) == sorted(str(sent.url) for sent in recorder.requests)
    assert len(robots.checked) == 2


async def test_a_disallowed_api_is_not_called() -> None:
    recorder = Recorder()
    provider = brave(recorder, robots=Robots(disallow="/news/"))
    with pytest.raises(CrawlerError) as raised:
        await search(provider, request(sources=["news"]))
    await provider.aclose()
    assert raised.value.code is ErrorCode.ROBOTS_DISALLOWED
    assert recorder.requests == []
