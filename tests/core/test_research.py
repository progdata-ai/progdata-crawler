import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.find.local import LocalSearch
from progdata_crawler.core.research.agent import research
from progdata_crawler.models import ErrorCode, FetchRequest, SearchSource
from progdata_crawler.models.research import ResearchRequest
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.fake_search import FakeSearch, web_hits
from tests.support.site_server import SiteServer

SOURCE_LINE = re.compile(r"^\[(\d+)\] (.*)$", re.MULTILINE)


def researcher(
    answer: Callable[[str, list[str]], dict[str, Any]],
    queries: list[str] | None = None,
) -> FakeLlm:
    """Plans `queries`, then answers with `answer(prompt, source titles)`."""

    def reply(prompt: str) -> dict[str, Any]:
        if "Sources:" in prompt:
            titles = [m.group(2) for m in SOURCE_LINE.finditer(prompt)]
            return answer(prompt, titles)
        return {"queries": queries or ["sourdough"]}

    return FakeLlm(reply)


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    async with Crawler(Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)) as instance:
        # Fill the local index the way a user would: by fetching pages.
        for path in ("/article.html", "/docs/", "/docs/faq.html", "/docs/configuration.html"):
            await instance.fetch(FetchRequest.model_validate({"url": test_site.url(path)}))
        yield instance


async def test_answer_with_sources_that_were_read(crawler: Crawler, test_site: SiteServer) -> None:
    fake = researcher(
        lambda prompt, titles: {
            "answer": "Sourdough takes three days [1]. It is easy [7].",
            "missing": [],
        }
    )
    result = await research(
        crawler,
        LocalSearch(crawler.store),
        ResearchRequest(question="How is sourdough made?"),
        llm=fake.llm(),
    )
    assert result.answer == "Sourdough takes three days [1]. It is easy ."
    assert [s.url for s in result.sources] == [test_site.url("/article.html")]
    assert result.sources[0].title == "How we bake our sourdough"
    assert result.complete
    assert any("[7]" in note or "7" in note for note in result.notes)
    assert (result.usage.searches, result.usage.llm_calls, result.usage.rounds) == (1, 2, 1)
    # The sources reached the LLM as data in the prompt.
    answer_prompt = fake.prompts[-1]
    assert answer_prompt.startswith("Question: How is sourdough made?")
    assert "[1] How we bake our sourdough" in answer_prompt


async def test_a_second_round_looks_for_what_is_missing(crawler: Crawler) -> None:
    rounds: list[int] = []

    def answer(prompt: str, titles: list[str]) -> dict[str, Any]:
        rounds.append(len(titles))
        if len(rounds) == 1:
            return {"answer": "Partly known [1].", "missing": ["configuration"]}
        return {"answer": "Known [1] and configured [2].", "missing": []}

    result = await research(
        crawler,
        LocalSearch(crawler.store),
        ResearchRequest(question="How do I bake and configure?"),
        llm=researcher(answer).llm(),
    )
    assert result.usage.rounds == 2
    assert rounds[1] > rounds[0]  # the second round read more pages
    assert len(result.sources) == 2 and result.complete


async def test_budget_ends_the_research(crawler: Crawler) -> None:
    fake = researcher(
        lambda prompt, titles: {"answer": "Not sure [1].", "missing": ["more"]},
        queries=["sourdough", "docs", "faq"],
    )
    result = await research(
        crawler,
        LocalSearch(crawler.store),
        ResearchRequest(question="Everything?", max_searches=1, max_rounds=3),
        llm=fake.llm(),
    )
    assert result.usage.searches == 1
    assert not result.complete
    assert any("budget" in note for note in result.notes)


async def test_disallowed_pages_are_never_sources(crawler: Crawler, test_site: SiteServer) -> None:
    provider = FakeSearch(
        {
            SearchSource.WEB: web_hits(
                test_site.url("/private/staff.html"), test_site.url("/article.html")
            )
        }
    )
    fake = researcher(
        lambda prompt, titles: {"answer": f"{len(titles)} source [1] [2].", "missing": []}
    )
    result = await research(
        crawler, provider, ResearchRequest(question="Who works there?"), llm=fake.llm()
    )
    assert [s.url for s in result.sources] == [test_site.url("/article.html")]
    assert result.answer.startswith("1 source")


async def test_nothing_found(crawler: Crawler) -> None:
    fake = researcher(lambda prompt, titles: {"answer": "I could not find this.", "missing": []})
    result = await research(
        crawler, FakeSearch(), ResearchRequest(question="Unknowable?"), llm=fake.llm()
    )
    assert result.sources == () and not result.complete
    assert "no page could be read for this question" in result.notes


async def test_without_an_llm(tmp_path: Path) -> None:
    async with Crawler(Settings(data_dir=tmp_path)) as crawler:
        with pytest.raises(CrawlerError) as raised:
            await research(crawler, FakeSearch(), ResearchRequest(question="Anything?"))
    assert raised.value.code is ErrorCode.LLM_UNAVAILABLE
