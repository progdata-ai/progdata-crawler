import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.find import create_provider, search
from progdata_crawler.core.find.searxng import SearxngSearch
from progdata_crawler.models import ErrorCode, FindRequest, SearchSource
from progdata_crawler.settings import Settings, default_user_agent

RECORDED = Path(__file__).parent.parent / "support" / "search"
BASE = "http://searxng.internal:8080"


def recorded(category: str) -> dict[str, Any]:
    return json.loads((RECORDED / f"searxng_{category}.json").read_text(encoding="utf-8"))


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._respond is not None:
            return self._respond(request)
        return httpx.Response(200, json=recorded(request.url.params["categories"]))


def searxng(recorder: Recorder, base_url: str | None = BASE) -> SearxngSearch:
    return SearxngSearch(
        base_url, user_agent=default_user_agent(), transport=httpx.MockTransport(recorder)
    )


def request(**fields: Any) -> FindRequest:
    return FindRequest.model_validate({"query": "sourdough starter", **fields})


async def test_web_news_and_images() -> None:
    recorder = Recorder()
    provider = searxng(recorder)
    result = await search(
        provider,
        request(sources=["web", "news", "images"], time_range="month", language="nb", country="NO"),
    )
    await provider.aclose()

    assert result.provider == "searxng"
    assert [(hit.source, hit.url) for hit in result.hits] == [
        (SearchSource.WEB, "https://bakery.example/starter"),
        (SearchSource.WEB, "https://flour.example/rye"),
        (SearchSource.NEWS, "https://news.example/sourdough-boom"),
        (SearchSource.IMAGES, "https://bakery.example/starter"),
    ]
    assert result.hits[0].snippet == "Feed it flour & water every day."
    assert result.hits[1].snippet == "Rye ferments faster than wheat."
    assert result.hits[0].published is None
    assert result.hits[2].published == "2026-11-16T10:00:00"
    assert result.hits[3].image_url == "https://img.bakery.example/jar.jpg"

    for sent in recorder.requests:
        assert str(sent.url).startswith(BASE + "/search?")
        assert sent.url.params["format"] == "json"
        assert sent.url.params["time_range"] == "month"
        assert sent.url.params["language"] == "nb-NO"
    assert {sent.url.params["categories"] for sent in recorder.requests} == {
        "general",
        "news",
        "images",
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (403, ErrorCode.SEARCH_UNAVAILABLE),
        (429, ErrorCode.RATE_LIMITED),
        (502, ErrorCode.SEARCH_UNAVAILABLE),
    ],
)
async def test_errors(code: int, expected: ErrorCode) -> None:
    provider = searxng(Recorder(lambda _: httpx.Response(code, text="no")))
    with pytest.raises(CrawlerError) as raised:
        await search(provider, request())
    await provider.aclose()
    assert raised.value.code is expected


async def test_not_configured() -> None:
    recorder = Recorder()
    provider = searxng(recorder, base_url=None)
    with pytest.raises(CrawlerError) as raised:
        await search(provider, request())
    await provider.aclose()
    assert raised.value.code is ErrorCode.SEARCH_UNAVAILABLE
    assert "PDC_SEARXNG_URL" in raised.value.message
    assert recorder.requests == []


async def test_switching_provider_is_configuration_only(tmp_path: Path) -> None:
    brave = create_provider(Settings(data_dir=tmp_path))
    local = create_provider(
        Settings(data_dir=tmp_path, search_provider="searxng", searxng_url=BASE)  # pyright: ignore[reportArgumentType]
    )
    assert (brave.name, local.name) == ("brave", "searxng")
    await brave.aclose()
    await local.aclose()
