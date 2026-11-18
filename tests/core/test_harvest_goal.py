import re
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.harvest import harvest_handler
from progdata_crawler.core.harvest.goal import rank_links
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.models import Job, JobKind, JobResult, JobStatus
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.site_server import SiteServer

LINK_LINE = re.compile(r"^(\d+)\. (.*) — (\S+)$")


def keyword_ranker(*keywords: str) -> FakeLlm:
    """Ranks links whose text or URL has one of the keywords first, in keyword order."""

    def reply(prompt: str) -> dict[str, Any]:
        ranked: list[int] = []
        for keyword in keywords:
            for line in prompt.splitlines():
                match = LINK_LINE.match(line)
                if match and keyword in line.lower() and int(match.group(1)) not in ranked:
                    ranked.append(int(match.group(1)))
        return {"order": ranked}

    return FakeLlm(reply)


def page(*links: tuple[str, str]) -> str:
    anchors = "".join(f'<li><a href="{href}">{text}</a></li>' for href, text in links)
    return f"<html><head><title>Shop</title></head><body><ul>{anchors}</ul></body></html>"


@pytest.fixture
def shop(test_site: SiteServer) -> str:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    blog = [(f"/shop/blog-{n}", f"Blog post {n}") for n in range(1, 6)]
    start = test_site.add(
        "/shop/", page(*blog, ("/shop/about", "About us"), ("/shop/pricing", "Pricing"))
    )
    for href, _ in blog:
        test_site.add(href, page(("/shop/", "Home")))
    test_site.add("/shop/about", page(("/shop/team", "Team")))
    test_site.add("/shop/team", page())
    test_site.add("/shop/pricing", page(("/shop/plans", "Compare plans"), ("/shop/", "Home")))
    test_site.add("/shop/plans", page(("/shop/", "Home")))
    return start


async def run(tmp_path: Path, fake: FakeLlm | None, **request: Any) -> tuple[Job, list[JobResult]]:
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with (
        Crawler(settings, store_path=":memory:", llm=fake.llm() if fake else None) as crawler,
        JobManager(
            settings, {JobKind.HARVEST: harvest_handler(crawler)}, page_store=crawler.store
        ) as jobs,
    ):
        job = await jobs.submit(JobKind.HARVEST, {"sitemaps": False, **request})
        finished = await jobs.wait(job.id)
        assert finished is not None
        return finished, await jobs.results(job.id, limit=1000)


def paths(results: list[JobResult], test_site: SiteServer) -> list[str]:
    return [r.url.removeprefix(test_site.base_url) for r in results]


async def test_relevant_pages_come_first(tmp_path: Path, test_site: SiteServer, shop: str) -> None:
    fake = keyword_ranker("pricing", "plans")
    job, results = await run(tmp_path, fake, url=shop, goal="pricing and plans", max_pages=3)
    assert job.status is JobStatus.SUCCEEDED
    assert paths(results, test_site) == ["/shop/", "/shop/pricing", "/shop/plans"]
    # Only link texts and URLs went to the LLM.
    assert all("Goal: pricing and plans" in prompt for prompt in fake.prompts)
    assert all("<html" not in prompt for prompt in fake.prompts)


async def test_without_a_goal_it_is_breadth_first(
    tmp_path: Path, test_site: SiteServer, shop: str
) -> None:
    _, results = await run(tmp_path, None, url=shop, max_pages=3)
    assert paths(results, test_site) == ["/shop/", "/shop/blog-1", "/shop/blog-2"]


async def test_budget_limits_llm_calls(tmp_path: Path, test_site: SiteServer, shop: str) -> None:
    fake = keyword_ranker("pricing", "plans")
    _, results = await run(tmp_path, fake, url=shop, goal="pricing", goal_budget=1, max_pages=20)
    assert len(fake.prompts) == 1
    visited = paths(results, test_site)
    assert visited[:2] == ["/shop/", "/shop/pricing"]
    # Everything is still crawled once the budget is spent, breadth first.
    assert set(visited) == {
        "/shop/",
        "/shop/pricing",
        "/shop/plans",
        "/shop/about",
        "/shop/team",
    } | {f"/shop/blog-{n}" for n in range(1, 6)}


async def test_llm_failure_falls_back_to_breadth_first(
    tmp_path: Path, test_site: SiteServer, shop: str
) -> None:
    job, results = await run(tmp_path, None, url=shop, goal="pricing", max_pages=3)
    assert job.status is JobStatus.SUCCEEDED  # no LLM configured: not a failure
    assert paths(results, test_site) == ["/shop/", "/shop/blog-1", "/shop/blog-2"]


async def test_rank_links_scores() -> None:
    fake = FakeLlm(lambda prompt: {"order": [2, 0, 2, 9]})
    scores = await rank_links(
        fake.llm(),
        "goal",
        [("https://a.test/0", "a"), ("https://a.test/1", "b"), ("https://a.test/2", "c")],
    )
    assert scores[2] > scores[0] > scores[1] == 0.0
