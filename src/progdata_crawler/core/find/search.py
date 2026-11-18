"""Run a find request against the configured provider."""

import asyncio

from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.find.brave import BraveSearch
from progdata_crawler.core.find.provider import SearchProvider
from progdata_crawler.core.urls import UrlDeduplicator
from progdata_crawler.models.find import FindRequest, FindResult, SearchHit, SearchSource
from progdata_crawler.settings import SearchProviderName, Settings


def create_provider(settings: Settings) -> SearchProvider:
    """The provider `PDC_SEARCH_PROVIDER` names. Calls to it go through the address guard."""
    guard = AddressGuard(allow_private=bool(settings.allow_private_networks))
    match settings.search_provider:
        case SearchProviderName.BRAVE:
            key = settings.brave_api_key
            return BraveSearch(
                key.get_secret_value() if key is not None else None,
                user_agent=settings.user_agent,
                guard=guard,
            )


async def search(provider: SearchProvider, request: FindRequest) -> FindResult:
    """Search every requested source at once. Hits are in source order, without duplicate
    pages (images keep their own entries, since one page can hold many images)."""
    per_source = await asyncio.gather(
        *(provider.search(request, source) for source in request.sources)
    )
    seen = UrlDeduplicator()
    hits: list[SearchHit] = []
    for source_hits in per_source:
        for hit in source_hits:
            key = hit.image_url if hit.source is SearchSource.IMAGES and hit.image_url else hit.url
            if seen.add(key):
                hits.append(hit)
    return FindResult(query=request.query, provider=provider.name, hits=tuple(hits))
