"""The core service: fetch a page politely and return the requested outputs.

`Crawler` owns the shared parts (HTTP client, browser, robots.txt cache, rate limiter,
page cache) and runs one request through them:

1. Validate the options.
2. Return a cached result if one is fresh enough.
3. Check robots.txt and wait for the per-domain rate limit.
4. Fetch over plain HTTP, or go straight to the browser when the request needs it.
   If the plain HTML turns out to be a JavaScript shell, render it in the browser.
5. Extract the requested outputs and store the result (unless `retain` is `none`).

Entry points call `fetch_envelope`, which never raises: every outcome is an envelope.
"""

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.extract.clean_html import clean_html
from progdata_crawler.core.extract.images import extract_images
from progdata_crawler.core.extract.links import extract_links
from progdata_crawler.core.extract.markdown import to_markdown
from progdata_crawler.core.extract.meta import extract_metadata
from progdata_crawler.core.fetch.browser import BrowserPool
from progdata_crawler.core.fetch.detect import (
    check_render_options,
    should_render_after,
    should_render_first,
)
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.fetch.http import HttpFetcher, raise_for_blocked
from progdata_crawler.core.polite.ratelimit import BACKOFF_STATUSES, DomainRateLimiter
from progdata_crawler.core.polite.robots import RobotsPolicy
from progdata_crawler.core.store.cache import PageCache
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.models.envelope import Envelope, ErrorCode, ErrorInfo, Meta
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.options import Output, PageOptions
from progdata_crawler.models.page import Page
from progdata_crawler.settings import Settings

log = logging.getLogger(__name__)

NOT_YET_AVAILABLE = frozenset({Output.SUMMARY, Output.STRUCTURED, Output.DIFF})
"""Outputs that later phases add."""


@dataclass(frozen=True)
class FetchOutcome:
    page: Page
    cached: bool
    rendered: bool


@dataclass(frozen=True)
class _Source:
    """The document we extract from, however it was fetched."""

    url: str
    status: int | None
    content_type: str
    html: str
    rendered: bool
    screenshot: bytes | None = None


def _check_outputs(options: PageOptions) -> None:
    unavailable = sorted(output.value for output in NOT_YET_AVAILABLE & set(options.outputs))
    if unavailable:
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            f"these outputs are not available yet: {', '.join(unavailable)}",
            outputs=unavailable,
        )


def build_page(source: _Source, options: PageOptions) -> Page:
    """Extract the requested outputs from a fetched document. CPU-bound."""
    wanted = set(options.outputs)
    html, url = source.html, source.url
    return Page(
        url=url,
        markdown=(
            to_markdown(html, url, content=options.content, tags=options.tags)
            if Output.MARKDOWN in wanted
            else None
        ),
        html=(
            clean_html(html, url, content=options.content, tags=options.tags)
            if Output.HTML in wanted
            else None
        ),
        raw_html=html if Output.RAW_HTML in wanted else None,
        links=extract_links(html, url) if Output.LINKS in wanted else None,
        images=extract_images(html, url) if Output.IMAGES in wanted else None,
        metadata=(
            extract_metadata(html, url, status=source.status, content_type=source.content_type)
            if Output.METADATA in wanted
            else None
        ),
        screenshot=(
            base64.b64encode(source.screenshot).decode()
            if Output.SCREENSHOT in wanted and source.screenshot is not None
            else None
        ),
    )


class Crawler:
    """Use as an async context manager. Safe to share between concurrent requests."""

    def __init__(self, settings: Settings, *, store_path: Path | str | None = None) -> None:
        self.settings = settings
        # Unset means protected: entry points that run on the user's own machine opt out.
        self._guard = AddressGuard(allow_private=bool(settings.allow_private_networks))
        self._http = HttpFetcher(
            settings.user_agent, timeout_ms=settings.timeout_ms, guard=self._guard
        )
        self._browser = BrowserPool(
            settings.user_agent,
            max_pages=settings.browser_max_pages,
            executable_path=settings.browser_executable,
            guard=self._guard,
        )
        self._robots = RobotsPolicy(self._http.fetch_robots, allowlist=settings.robots_allowlist)
        self._rate = DomainRateLimiter(min_interval_s=settings.rate_limit_interval_s)
        self._store = PageStore(store_path if store_path is not None else settings.cache_db_path)
        self._cache = PageCache(self._store)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._browser.aclose()
        await self._http.aclose()
        self._store.close()

    @property
    def store(self) -> PageStore:
        return self._store

    async def fetch(self, request: FetchRequest) -> FetchOutcome:
        """Fetch one page. Raises `CrawlerError` for every expected failure."""
        options = PageOptions.model_validate(request.model_dump(exclude={"url"}))
        check_render_options(options)
        _check_outputs(options)
        url = str(request.url)

        cached = await self._cache.get(url, options)
        if cached is not None:
            return FetchOutcome(cached.page, cached=True, rendered=cached.rendered)

        source = await self._fetch_source(url, options)
        page = await asyncio.to_thread(build_page, source, options)
        await self._cache.put(url, options, page, rendered=source.rendered)
        return FetchOutcome(page, cached=False, rendered=source.rendered)

    async def _polite_wait(self, url: str, options: PageOptions) -> None:
        await self._guard.check_url(url)
        await self._robots.check(url)
        await self._rate.acquire(
            url,
            crawl_delay=await self._robots.crawl_delay(url),
            max_wait_s=options.timeout_ms / 1000,
        )

    def _raise_if_throttled(self, url: str, status: int | None, retry_after: str | None) -> None:
        if status is None:
            return
        delay = self._rate.record_response(url, status, retry_after)
        if status in BACKOFF_STATUSES:
            raise CrawlerError(
                ErrorCode.RATE_LIMITED,
                "the site asked us to slow down; try again later",
                url=url,
                status=status,
                retry_after_s=delay,
            )

    async def _fetch_source(self, url: str, options: PageOptions) -> _Source:
        await self._polite_wait(url, options)
        if should_render_first(options):
            return await self._render(url, options)

        response = await self._http.fetch(url, timeout_ms=options.timeout_ms, locale=options.locale)
        self._raise_if_throttled(url, response.status, response.headers.get("retry-after"))
        raise_for_blocked(response)
        if response.url != url:
            await self._robots.check(response.url)  # redirected: the target must allow us too

        html = response.text()
        if should_render_after(options, html):
            await self._polite_wait(response.url, options)
            return await self._render(response.url, options)
        return _Source(
            url=response.url,
            status=response.status,
            content_type=response.content_type,
            html=html,
            rendered=False,
        )

    async def _render(self, url: str, options: PageOptions) -> _Source:
        page = await self._browser.render(
            url,
            timeout_ms=options.timeout_ms,
            wait_ms=options.wait_ms,
            device=options.device,
            locale=options.locale,
            screenshot=Output.SCREENSHOT in options.outputs,
            full_page=options.full_page_screenshot,
        )
        self._raise_if_throttled(url, page.status, None)
        if page.status is not None and page.status in (401, 403, 451):
            raise CrawlerError(
                ErrorCode.BLOCKED_BY_SITE,
                "the site refused to serve this page to our crawler",
                url=page.url,
                status=page.status,
            )
        if page.url != url:
            await self._robots.check(page.url)
        return _Source(
            url=page.url,
            status=page.status,
            content_type=page.content_type,
            html=page.html,
            rendered=True,
            screenshot=page.screenshot,
        )

    async def fetch_envelope(
        self, request: FetchRequest, *, request_id: str | None = None
    ) -> Envelope[Page]:
        """Like `fetch`, but every outcome, including errors, is an envelope."""
        request_id = request_id or uuid.uuid4().hex
        started = time.monotonic()

        def meta(cached: bool = False, rendered: bool = False) -> Meta:
            took_ms = round((time.monotonic() - started) * 1000)
            return Meta(request_id=request_id, took_ms=took_ms, cached=cached, rendered=rendered)

        try:
            outcome = await self.fetch(request)
        except CrawlerError as error:
            log.info(
                "fetch failed",
                extra={"request_id": request_id, "url": str(request.url), "code": error.code.value},
            )
            return Envelope[Page](meta=meta(), error=error.to_error_info())
        except Exception:
            log.exception("unexpected error", extra={"request_id": request_id})
            return Envelope[Page](
                meta=meta(),
                error=ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error"),
            )
        result = meta(cached=outcome.cached, rendered=outcome.rendered)
        log.info(
            "page fetched",
            extra={
                "request_id": request_id,
                "url": outcome.page.url,
                "took_ms": result.took_ms,
                "cached": outcome.cached,
                "rendered": outcome.rendered,
            },
        )
        return Envelope[Page](data=outcome.page, meta=result)
