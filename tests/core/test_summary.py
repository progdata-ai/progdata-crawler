from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.jobs.bulk import bulk_handler
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.models import ErrorCode, FetchRequest, JobKind
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.site_server import PDF_TEXT, SiteServer


@pytest.fixture
def fake() -> FakeLlm:
    return FakeLlm(lambda prompt: f"Summary of {len(prompt)} characters.")


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer, fake: FakeLlm) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    async with Crawler(settings, store_path=":memory:", llm=fake.llm()) as instance:
        yield instance


def request(url: str, **options: Any) -> FetchRequest:
    return FetchRequest.model_validate({"url": url, **options})


async def test_summary_per_page(crawler: Crawler, test_site: SiteServer, fake: FakeLlm) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/article.html"), outputs=["summary"])
    )
    assert envelope.data is not None, envelope.error
    page = envelope.data
    assert page.summary is not None and page.summary.startswith("Summary of")
    assert page.markdown is None  # not asked for, but used for the summary
    assert fake.prompts[0].startswith("Title: How we bake our sourdough")
    assert "sourdough" in fake.prompts[0]
    assert page.output_errors is None


async def test_summaries_are_cached(crawler: Crawler, test_site: SiteServer, fake: FakeLlm) -> None:
    options: dict[str, Any] = {"outputs": ["markdown", "summary"]}
    first = await crawler.fetch_envelope(request(test_site.url("/article.html"), **options))
    second = await crawler.fetch_envelope(request(test_site.url("/article.html"), **options))
    assert second.meta.cached
    assert first.data is not None and second.data is not None
    assert second.data.summary == first.data.summary
    assert len(fake.prompts) == 1


async def test_documents_are_summarized(
    crawler: Crawler, test_site: SiteServer, fake: FakeLlm
) -> None:
    envelope = await crawler.fetch_envelope(
        request(test_site.url("/files/sample.pdf"), outputs=["summary"])
    )
    assert envelope.data is not None and envelope.data.summary is not None
    assert PDF_TEXT in fake.prompts[0]


async def test_without_an_llm_the_page_still_comes_back(
    tmp_path: Path, test_site: SiteServer
) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    async with Crawler(Settings(data_dir=tmp_path), store_path=":memory:") as crawler:
        url = test_site.url("/article.html")
        first = await crawler.fetch_envelope(request(url, outputs=["markdown", "summary"]))
        second = await crawler.fetch_envelope(request(url, outputs=["markdown", "summary"]))
    assert first.error is None and first.data is not None
    assert first.data.markdown is not None and first.data.summary is None
    assert first.data.output_errors is not None
    error = first.data.output_errors["summary"]
    assert error.code is ErrorCode.LLM_UNAVAILABLE
    assert "PDC_LLM_PROVIDER" in error.message
    assert not second.meta.cached  # pages with a missing output are not cached


async def test_bulk_summarizes_every_page(
    tmp_path: Path, test_site: SiteServer, crawler: Crawler, fake: FakeLlm
) -> None:
    urls = [test_site.url("/article.html"), test_site.url("/docs/")]
    async with JobManager(
        crawler.settings, {JobKind.BULK: bulk_handler(crawler)}, page_store=crawler.store
    ) as jobs:
        job = await jobs.submit(JobKind.BULK, {"urls": urls, "outputs": ["summary"]})
        await jobs.wait(job.id)
        results = await jobs.results(job.id)
    assert len(results) == 2
    assert all(result.page is not None and result.page.summary for result in results)
    assert len(fake.prompts) == 2
