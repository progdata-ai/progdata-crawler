"""retain: none leaves no page content behind: not in the cache, the full-text index,
job results once read, or the logs."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover import discover
from progdata_crawler.core.find import find
from progdata_crawler.core.jobs.handlers import job_handlers
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.session.browser_session import SessionManager
from progdata_crawler.models import (
    DiscoverRequest,
    FetchRequest,
    FindRequest,
    JobKind,
    Retention,
    SearchSource,
    SessionOpenRequest,
    SessionStepRequest,
)
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.fake_search import FakeSearch, web_hits
from tests.support.site_server import SiteServer

MARKER = "zebracornflakes"
"""A word that appears only on the test pages, so finding it means content was kept."""

PAGE = (
    "<html><head><title>Private notes</title></head><body><main>"
    f"<h1>Private notes</h1><p>The secret ingredient is {MARKER}.</p>"
    f'<a href="/retain/two">More about {MARKER}</a>'
    "</main></body></html>"
)


@pytest.fixture
def pages(test_site: SiteServer) -> list[str]:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    return [test_site.add("/retain/one", PAGE), test_site.add("/retain/two", PAGE)]


@pytest.fixture
async def crawler(tmp_path: Path) -> AsyncIterator[Crawler]:
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    fake = FakeLlm(lambda prompt: f"Summary mentioning {MARKER}.")
    async with Crawler(settings, llm=fake.llm()) as instance:
        yield instance


def stored_text(data_dir: Path) -> str:
    """Everything in every table of every database under `data_dir`."""
    dumped: list[str] = []
    for database in data_dir.glob("*.sqlite3"):
        with sqlite3.connect(database) as db:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                for row in db.execute(f'SELECT * FROM "{table}"'):
                    dumped.append(repr(row))
    return "\n".join(dumped)


NONE: dict[str, Any] = {"retain": "none"}


async def test_fetch_with_summary(crawler: Crawler, pages: list[str], tmp_path: Path) -> None:
    request = FetchRequest.model_validate(
        {"url": pages[0], "outputs": ["markdown", "summary", "links"], **NONE}
    )
    page = (await crawler.fetch(request)).page
    assert page.markdown is not None and MARKER in page.markdown
    assert MARKER not in stored_text(tmp_path)
    again = await crawler.fetch(request)
    assert not again.cached


async def test_default_retention_does_store(
    crawler: Crawler, pages: list[str], tmp_path: Path
) -> None:
    """The check above would be worthless if the marker never reached the database."""
    await crawler.fetch(FetchRequest.model_validate({"url": pages[0]}))
    assert MARKER in stored_text(tmp_path)


async def test_discover(crawler: Crawler, pages: list[str], tmp_path: Path) -> None:
    result = await discover(
        crawler, DiscoverRequest.model_validate({"url": pages[0], "sitemaps": False, **NONE})
    )
    assert [item.url for item in result.urls] == [pages[1]]
    assert MARKER not in stored_text(tmp_path)


async def test_find_with_fetch(crawler: Crawler, pages: list[str], tmp_path: Path) -> None:
    provider = FakeSearch({SearchSource.WEB: web_hits(*pages)})
    request = FindRequest.model_validate(
        {"query": "notes", "fetch": {"outputs": ["markdown"], **NONE}}
    )
    result = await find(crawler, provider, request)
    assert all(hit.page is not None for hit in result.hits)
    assert MARKER not in stored_text(tmp_path)


@pytest.mark.parametrize("kind", [JobKind.HARVEST, JobKind.BULK])
async def test_jobs_once_read(
    crawler: Crawler, pages: list[str], tmp_path: Path, kind: JobKind
) -> None:
    request: dict[str, Any] = (
        {
            "url": pages[0],
            "sitemaps": False,
            "outputs": ["markdown", "structured"],
            "structure": {"schema": {"type": "object"}},
            **NONE,
        }
        if kind is JobKind.HARVEST
        else {"urls": pages, "outputs": ["markdown", "summary"], **NONE}
    )
    async with JobManager(
        crawler.settings, job_handlers(crawler), page_store=crawler.store
    ) as jobs:
        job = await jobs.submit(kind, request, retain=Retention.NONE)
        await jobs.wait(job.id)
        assert MARKER in stored_text(tmp_path)  # results wait to be read
        results = await jobs.results(job.id, limit=100)
        assert len(results) == 2
        assert MARKER not in stored_text(tmp_path)


async def test_sessions(crawler: Crawler, pages: list[str], tmp_path: Path) -> None:
    async with SessionManager(crawler) as sessions:
        opened = await sessions.open(SessionOpenRequest.model_validate({"url": pages[0]}))
        await sessions.step(opened.id, SessionStepRequest())
        await sessions.close(opened.id)
    assert MARKER not in stored_text(tmp_path)


async def test_nothing_from_the_page_is_logged(
    crawler: Crawler, pages: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await crawler.fetch(
            FetchRequest.model_validate(
                {"url": pages[0], "outputs": ["markdown", "summary"], **NONE}
            )
        )
    for record in caplog.records:
        assert MARKER not in record.getMessage() + repr(record.__dict__)
