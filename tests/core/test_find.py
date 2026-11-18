from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.find import find
from progdata_crawler.models import ErrorCode, FindRequest, SearchHit, SearchSource
from progdata_crawler.settings import Settings
from tests.support.fake_search import FakeSearch, web_hits
from tests.support.site_server import PDF_TEXT, SiteServer


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    async with Crawler(Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)) as instance:
        yield instance


async def test_without_fetch_only_hits(crawler: Crawler, test_site: SiteServer) -> None:
    provider = FakeSearch({SearchSource.WEB: web_hits(test_site.url("/article.html"))})
    result = await find(crawler, provider, FindRequest(query="sourdough"))
    assert [hit.url for hit in result.hits] == [test_site.url("/article.html")]
    assert result.hits[0].page is None and result.hits[0].error is None


async def test_fetch_gives_markdown_per_hit(crawler: Crawler, test_site: SiteServer) -> None:
    image = SearchHit(
        url=test_site.url("/article.html"),
        title="A picture",
        source=SearchSource.IMAGES,
        image_url=test_site.url("/bread.jpg"),
    )
    provider = FakeSearch(
        {
            SearchSource.WEB: web_hits(
                test_site.url("/article.html"),
                test_site.url("/files/sample.pdf"),
                test_site.url("/private/staff.html"),
                test_site.url("/status/403"),
            ),
            SearchSource.IMAGES: [image],
        }
    )
    request = FindRequest.model_validate(
        {"query": "sourdough", "sources": ["web", "images"], "fetch": {"outputs": ["markdown"]}}
    )
    result = await find(crawler, provider, request)

    article, pdf, private, blocked, picture = result.hits
    assert article.page is not None and article.page.markdown is not None
    assert article.page.markdown.startswith("# How we bake our sourdough")
    assert pdf.page is not None and pdf.page.markdown == PDF_TEXT
    assert private.error is not None and private.error.code is ErrorCode.ROBOTS_DISALLOWED
    assert blocked.error is not None and blocked.error.code is ErrorCode.BLOCKED_BY_SITE
    # Image hits are not fetched.
    assert (picture.page, picture.error) == (None, None)
    # The page options apply to every hit.
    assert article.page.metadata is None
