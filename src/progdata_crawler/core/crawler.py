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
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from progdata_crawler.core.convert.documents import (
    DOCUMENT_TYPES,
    GENERIC_TYPES,
    convert_bytes,
    detect_type,
)
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.extract.clean_html import clean_html
from progdata_crawler.core.extract.images import extract_images
from progdata_crawler.core.extract.links import extract_links
from progdata_crawler.core.extract.markdown import to_markdown
from progdata_crawler.core.extract.meta import extract_metadata
from progdata_crawler.core.extract.summary import summarize
from progdata_crawler.core.fetch.browser import BrowserPool, RawCapture
from progdata_crawler.core.fetch.detect import (
    check_render_options,
    should_render_after,
    should_render_first,
)
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.fetch.http import (
    HTML_TYPES,
    HttpFetcher,
    HttpResponse,
    raise_for_blocked,
)
from progdata_crawler.core.polite.ratelimit import BACKOFF_STATUSES, DomainRateLimiter
from progdata_crawler.core.polite.robots import RobotsPolicy, RobotsRules
from progdata_crawler.core.store.cache import PageCache
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.core.structure.extract import extract
from progdata_crawler.core.structure.llm import Llm, create_llm
from progdata_crawler.models.convert import ConvertResult
from progdata_crawler.models.envelope import Envelope, ErrorCode, ErrorInfo, Meta
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.options import Output, PageOptions
from progdata_crawler.models.page import Page, PageMetadata
from progdata_crawler.models.steps import Capture
from progdata_crawler.settings import Settings

log = logging.getLogger(__name__)

PAGE_TYPES = HTML_TYPES | DOCUMENT_TYPES | GENERIC_TYPES
"""What fetch accepts: HTML pages, and documents it converts to markdown."""

NOT_YET_AVAILABLE = frozenset({Output.DIFF})
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
    captures: tuple[RawCapture, ...] = ()
    is_document: bool = False
    """A PDF, DOCX or XLSX response rather than an HTML page."""
    document: ConvertResult | None = None
    """The converted document, when markdown was asked for."""


def _check_outputs(options: PageOptions) -> None:
    unavailable = sorted(output.value for output in NOT_YET_AVAILABLE & set(options.outputs))
    if unavailable:
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            f"these outputs are not available yet: {', '.join(unavailable)}",
            outputs=unavailable,
        )
    if Output.STRUCTURED in options.outputs and options.structure is None:
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            "the structured output needs `structure`: a schema and an instruction",
        )


def build_page(source: _Source, options: PageOptions) -> Page:
    """Extract the requested outputs from a fetched document. CPU-bound."""
    wanted = set(options.outputs)
    html, url = source.html, source.url
    if source.is_document:
        return _document_page(source, wanted)
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
        captures=(
            tuple(_capture(raw, options) for raw in source.captures) if options.steps else None
        ),
    )


def _capture(raw: RawCapture, options: PageOptions) -> Capture:
    wanted = set(options.outputs)
    return Capture(
        step=raw.step,
        url=raw.url,
        markdown=(
            to_markdown(raw.html, raw.url, content=options.content, tags=options.tags)
            if Output.MARKDOWN in wanted
            else None
        ),
        screenshot=(
            base64.b64encode(raw.screenshot).decode()
            if Output.SCREENSHOT in wanted and raw.screenshot is not None
            else None
        ),
    )


def _document_page(source: _Source, wanted: set[Output]) -> Page:
    """A PDF, DOCX or XLSX file: markdown and metadata. The outputs that describe an HTML
    page (html, raw_html, links, images, screenshot) stay empty."""
    document = source.document
    metadata = PageMetadata(
        title=document.title if document is not None else None,
        status=source.status,
        content_type=source.content_type,
    )
    return Page(
        url=source.url,
        markdown=document.markdown if document is not None and Output.MARKDOWN in wanted else None,
        metadata=metadata if Output.METADATA in wanted else None,
    )


class Crawler:
    """Use as an async context manager. Safe to share between concurrent requests."""

    def __init__(
        self,
        settings: Settings,
        *,
        store_path: Path | str | None = None,
        llm: Llm | None = None,
    ) -> None:
        """`llm` replaces the LLM from settings (tests use a fake model)."""
        self.settings = settings
        self._llm = llm
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
            navigation_check=self._before_navigation,
        )
        self._robots = RobotsPolicy(self._http.fetch_robots)
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

        # Steps can change things on the site (a form sent), so their pages are never
        # served from or stored in the cache.
        cacheable = not options.steps
        if cacheable:
            cached = await self._cache.get(url, options)
            if cached is not None:
                return FetchOutcome(cached.page, cached=True, rendered=cached.rendered)

        source = await self._fetch_source(url, options)
        page = await asyncio.to_thread(build_page, source, options)
        if Output.SUMMARY in options.outputs or Output.STRUCTURED in options.outputs:
            page = await self._add_llm_outputs(page, source, options)
        # A page with a missing output is not cached, so the next request tries again
        # (for example once an LLM is configured).
        if cacheable and not page.output_errors:
            await self._cache.put(url, options, page, rendered=source.rendered)
        return FetchOutcome(page, cached=False, rendered=source.rendered)

    def llm(self) -> Llm:
        """The configured LLM. Raises `llm_unavailable` when there is none."""
        if self._llm is None:
            self._llm = create_llm(self.settings, robots=self.check_robots)
        return self._llm

    async def _text_for_llm(
        self, page: Page, source: _Source, options: PageOptions
    ) -> tuple[str, str | None]:
        """The page's markdown and title, made for the LLM when they were not asked for."""
        markdown = page.markdown
        if markdown is None:
            markdown = (
                source.document.markdown
                if source.document is not None
                else await asyncio.to_thread(
                    to_markdown, source.html, source.url, content=options.content, tags=options.tags
                )
            )
        if page.metadata is not None:
            title = page.metadata.title
        elif source.document is not None:
            title = source.document.title
        else:
            title = (await asyncio.to_thread(extract_metadata, source.html, source.url)).title
        return markdown, title

    async def _add_llm_outputs(self, page: Page, source: _Source, options: PageOptions) -> Page:
        """Add `summary` and `structured`. A failure goes into `output_errors`; the rest
        of the page is returned as usual."""
        markdown, title = await self._text_for_llm(page, source, options)
        update: dict[str, object] = {}
        errors = dict(page.output_errors or {})
        try:
            llm = self.llm()
            if Output.SUMMARY in options.outputs:
                try:
                    update["summary"] = await summarize(llm, markdown, title=title)
                except CrawlerError as error:
                    errors[Output.SUMMARY.value] = error.to_error_info()
            if Output.STRUCTURED in options.outputs and options.structure is not None:
                try:
                    update["structured"] = await extract(
                        llm, markdown, options.structure, title=title
                    )
                except CrawlerError as error:
                    errors[Output.STRUCTURED.value] = error.to_error_info()
        except CrawlerError as error:  # no LLM configured
            for output in (Output.SUMMARY, Output.STRUCTURED):
                if output in options.outputs:
                    errors[output.value] = error.to_error_info()
        if errors:
            update["output_errors"] = errors
        return page.model_copy(update=update)

    async def fetch_document(
        self,
        url: str,
        *,
        accept_types: Collection[str] | None,
        max_bytes: int | None = None,
        timeout_ms: int | None = None,
    ) -> HttpResponse:
        """Fetch a non-page document (a sitemap, a file) with the same politeness as pages:
        address guard, robots.txt and the per-domain rate limit. Raises `blocked_by_site`
        and `rate_limited` like page fetches."""
        options = PageOptions(timeout_ms=timeout_ms or self.settings.timeout_ms)
        await self._polite_wait(url, options)
        response = await self._http.fetch(
            url, timeout_ms=options.timeout_ms, accept_types=accept_types, max_bytes=max_bytes
        )
        self._raise_if_throttled(url, response.status, response.headers.get("retry-after"))
        raise_for_blocked(response)
        if response.url != url:
            await self._robots.check(response.url)
        return response

    async def check_robots(self, url: str) -> None:
        """Raise `address_not_allowed` or `robots_disallowed` unless we may request `url`.
        For requests that do not go through `fetch`, such as calls to search APIs."""
        await self._guard.check_url(url)
        await self._robots.check(url)

    async def robots_rules(self, url: str) -> RobotsRules:
        """The robots.txt rules for the origin of `url`."""
        await self._guard.check_url(url)
        return await self._robots.rules_for(url)

    async def _before_navigation(self, url: str) -> None:
        """Every page the browser goes to on its own (a redirect, a link a step clicked,
        a form a step sent) is checked against robots.txt and waits its turn."""
        await self._robots.check(url)
        await self._rate.acquire(
            url,
            crawl_delay=await self._robots.crawl_delay(url),
            max_wait_s=self.settings.timeout_ms / 1000,
        )

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

        response = await self._http.fetch(
            url,
            timeout_ms=options.timeout_ms,
            locale=options.locale,
            accept_types=PAGE_TYPES,
        )
        self._raise_if_throttled(url, response.status, response.headers.get("retry-after"))
        raise_for_blocked(response)
        if response.url != url:
            await self._robots.check(response.url)  # redirected: the target must allow us too

        if response.content_type not in HTML_TYPES:
            return await self._document_source(response, options)

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

    async def _document_source(self, response: HttpResponse, options: PageOptions) -> _Source:
        """Convert a document response, when markdown or a summary is asked for."""
        document = None
        if {Output.MARKDOWN, Output.SUMMARY, Output.STRUCTURED} & set(options.outputs):
            document = await convert_bytes(
                response.content,
                source=response.url,
                content_type=response.content_type,
                name=PurePosixPath(urlsplit(response.url).path).name or None,
            )
        elif detect_type(response.content, content_type=response.content_type) is None:
            raise CrawlerError(
                ErrorCode.UNSUPPORTED_TYPE,
                "not an HTML page or a PDF, DOCX or XLSX document",
                content_type=response.content_type,
            )
        return _Source(
            url=response.url,
            status=response.status,
            content_type=response.content_type,
            html="",
            rendered=False,
            is_document=True,
            document=document,
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
            steps=options.steps,
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
            captures=page.captures,
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
