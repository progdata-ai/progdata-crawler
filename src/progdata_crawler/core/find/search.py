"""Run a find request against the configured provider."""

import asyncio
import logging

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.find.brave import BraveSearch, RobotsCheck
from progdata_crawler.core.find.local import LocalSearch
from progdata_crawler.core.find.provider import SearchProvider
from progdata_crawler.core.store.sqlite import PageStore
from progdata_crawler.core.urls import UrlDeduplicator
from progdata_crawler.models.envelope import ErrorCode, ErrorInfo
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.find import FindRequest, FindResult, SearchHit, SearchSource
from progdata_crawler.models.options import PageOptions
from progdata_crawler.settings import SearchProviderName, Settings

log = logging.getLogger(__name__)


def create_provider(settings: Settings, *, robots: RobotsCheck, store: PageStore) -> SearchProvider:
    """The provider `PDC_SEARCH_PROVIDER` names. Calls to an API go through the address
    guard, and `robots` (normally `Crawler.check_robots`) checks each against robots.txt.
    The local provider searches `store` (normally `Crawler.store`)."""
    guard = AddressGuard(allow_private=bool(settings.allow_private_networks))
    match settings.search_provider:
        case SearchProviderName.BRAVE:
            key = settings.brave_api_key
            return BraveSearch(
                key.get_secret_value() if key is not None else None,
                user_agent=settings.user_agent,
                guard=guard,
                robots=robots,
                api_url=settings.brave_api_url,
            )
        case SearchProviderName.LOCAL:
            return LocalSearch(store)


async def search(provider: SearchProvider, request: FindRequest) -> FindResult:
    """Search every requested source at once. Hits are in source order, without duplicate
    pages (images keep their own entries, since one page can hold many images)."""
    supported = [source for source in request.sources if source in provider.sources]
    notes = tuple(
        f"the {provider.name} search provider has no {source.value} index"
        for source in request.sources
        if source not in provider.sources
    )
    per_source = await asyncio.gather(*(provider.search(request, source) for source in supported))
    seen = UrlDeduplicator()
    hits: list[SearchHit] = []
    for source_hits in per_source:
        for hit in source_hits:
            key = hit.image_url if hit.source is SearchSource.IMAGES and hit.image_url else hit.url
            if seen.add(key):
                hits.append(hit)
    return FindResult(query=request.query, provider=provider.name, hits=tuple(hits), notes=notes)


async def find(crawler: Crawler, provider: SearchProvider, request: FindRequest) -> FindResult:
    """Search, and with `request.fetch` also fetch every web and news hit through the
    crawler (robots.txt, rate limits, the address guard and the cache apply), at most
    `PDC_BULK_CONCURRENCY` at a time. A hit that cannot be fetched carries its error."""
    result = await search(provider, request)
    if request.fetch is None:
        return result
    options = request.fetch
    slots = asyncio.Semaphore(crawler.settings.bulk_concurrency)

    async def fetch_hit(hit: SearchHit) -> SearchHit:
        if hit.source is SearchSource.IMAGES:
            return hit
        async with slots:
            return await _fetched(crawler, hit, options)

    hits = await asyncio.gather(*(fetch_hit(hit) for hit in result.hits))
    return result.model_copy(update={"hits": tuple(hits)})


async def _fetched(crawler: Crawler, hit: SearchHit, options: PageOptions) -> SearchHit:
    try:
        fetch_request = FetchRequest.model_validate({**options.model_dump(), "url": hit.url})
        outcome = await crawler.fetch(fetch_request)
    except CrawlerError as error:
        return hit.model_copy(update={"error": error.to_error_info()})
    except ValueError:
        info = ErrorInfo(code=ErrorCode.INVALID_REQUEST, message="the hit has an invalid URL")
        return hit.model_copy(update={"error": info})
    except Exception:
        log.exception("unexpected error", extra={"url": hit.url})
        info = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error")
        return hit.model_copy(update={"error": info})
    return hit.model_copy(update={"page": outcome.page})
