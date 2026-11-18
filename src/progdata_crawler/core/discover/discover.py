"""Discover: list a site's URLs from its sitemaps and its start page.

Sitemaps come from robots.txt `Sitemap:` lines, or `/sitemap.xml` when there are none.
Sitemap indexes are followed. Every request goes through the `Crawler`, so the address
guard, robots.txt and the per-domain rate limit apply.
"""

import asyncio
import logging
import re
import time
import uuid
from urllib.parse import urlsplit

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.discover.sitemap import MAX_SITEMAP_BYTES, parse_sitemap
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.polite.robots import RobotsRules, origin_of
from progdata_crawler.core.urls import normalize_url
from progdata_crawler.models.discover import (
    DiscoveredUrl,
    DiscoverRequest,
    DiscoverResult,
    UrlSource,
)
from progdata_crawler.models.envelope import Envelope, ErrorCode, ErrorInfo, Meta
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.options import Output, PathFilter, Scope

log = logging.getLogger(__name__)

MAX_SITEMAPS = 50
"""Sitemap files read per discover, including those in sitemap indexes."""


def _base_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def in_scope(url: str, start_url: str, scope: Scope) -> bool:
    if scope is Scope.EXTERNAL:
        return True
    host = _base_host(url)
    base = _base_host(start_url)
    if scope is Scope.SITE:
        return host == base
    return host == base or host.endswith("." + base)


def matches_paths(url: str, paths: PathFilter) -> bool:
    path = urlsplit(url).path or "/"
    if paths.include and not any(re.search(pattern, path) for pattern in paths.include):
        return False
    return not any(re.search(pattern, path) for pattern in paths.exclude)


def matches_query(item: DiscoveredUrl, query: str | None) -> bool:
    if not query:
        return True
    haystack = f"{item.url} {item.text}".lower()
    return all(word in haystack for word in query.lower().split())


class _Collector:
    """Keeps discovered URLs in order of discovery, without duplicates."""

    def __init__(self) -> None:
        self.items: dict[str, DiscoveredUrl] = {}

    def add(
        self, url: str, source: UrlSource, *, text: str = "", lastmod: str | None = None
    ) -> None:
        try:
            key = normalize_url(url)
        except ValueError:
            return
        existing = self.items.get(key)
        if existing is None:
            self.items[key] = DiscoveredUrl(url=key, source=source, text=text, lastmod=lastmod)
        elif text and not existing.text:
            self.items[key] = existing.model_copy(update={"text": text})


async def _read_sitemaps(
    crawler: Crawler, start: str, rules: RobotsRules, found: _Collector, limit: int
) -> list[str]:
    queue = rules.sitemaps() or [origin_of(start) + "/sitemap.xml"]
    seen: set[str] = set()
    read: list[str] = []
    while queue and len(read) < MAX_SITEMAPS and len(found.items) <= limit:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        try:
            response = await crawler.fetch_document(
                sitemap_url, accept_types=None, max_bytes=MAX_SITEMAP_BYTES
            )
        except CrawlerError as error:
            log.info("sitemap skipped", extra={"url": sitemap_url, "code": error.code.value})
            continue
        if response.status != 200:
            continue
        try:
            content = await asyncio.to_thread(parse_sitemap, response.content)
        except CrawlerError as error:
            log.info("sitemap skipped", extra={"url": sitemap_url, "code": error.code.value})
            continue
        read.append(sitemap_url)
        queue.extend(content.sitemaps)
        for loc, lastmod in content.pages:
            found.add(loc, UrlSource.SITEMAP, lastmod=lastmod)
    return read


async def discover(crawler: Crawler, request: DiscoverRequest) -> DiscoverResult:
    """Find the URLs of a site. Raises `CrawlerError` when nothing can be read at all."""
    start = str(request.url)
    found = _Collector()
    sitemaps_read: list[str] = []
    rules = await crawler.robots_rules(start)

    if request.sitemaps:
        sitemaps_read = await _read_sitemaps(crawler, start, rules, found, request.limit)

    if request.links:
        try:
            outcome = await crawler.fetch(
                FetchRequest.model_validate(
                    {"url": start, "outputs": [Output.LINKS], "retain": request.retain}
                )
            )
        except CrawlerError:
            if not found.items:
                raise
        else:
            for link in outcome.page.links or ():
                found.add(link.url, UrlSource.LINK, text=link.text)

    start_origin = origin_of(start)
    kept = [
        item
        for item in found.items.values()
        if in_scope(item.url, start, request.scope)
        and matches_paths(item.url, request.paths)
        and matches_query(item, request.query)
        and (origin_of(item.url) != start_origin or rules.can_fetch(item.url))
    ]
    return DiscoverResult(
        url=normalize_url(start),
        urls=tuple(kept[: request.limit]),
        sitemaps=tuple(sitemaps_read),
        truncated=len(kept) > request.limit,
    )


async def discover_envelope(
    crawler: Crawler, request: DiscoverRequest, *, request_id: str | None = None
) -> Envelope[DiscoverResult]:
    """Like `discover`, but every outcome, including errors, is an envelope."""
    request_id = request_id or uuid.uuid4().hex
    started = time.monotonic()

    def meta() -> Meta:
        return Meta(request_id=request_id, took_ms=round((time.monotonic() - started) * 1000))

    try:
        result = await discover(crawler, request)
    except CrawlerError as error:
        log.info(
            "discover failed",
            extra={"request_id": request_id, "url": str(request.url), "code": error.code.value},
        )
        return Envelope[DiscoverResult](meta=meta(), error=error.to_error_info())
    except Exception:
        log.exception("unexpected error", extra={"request_id": request_id})
        return Envelope[DiscoverResult](
            meta=meta(),
            error=ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error"),
        )
    log.info(
        "site discovered",
        extra={"request_id": request_id, "url": result.url, "urls": len(result.urls)},
    )
    return Envelope[DiscoverResult](data=result, meta=meta())
