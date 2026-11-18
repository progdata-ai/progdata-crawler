"""SearXNG: an optional search provider for local use.

SearXNG gathers results from other search engines. Which engines it uses is up to
whoever runs it, and so is checking that those engines allow automated queries: the
`docker-compose.yml` setup enables only engines that do. The SearXNG address is set by
the operator (`PDC_SEARXNG_URL`), so it may be an internal address, such as the
SearXNG container next to ours.
"""

import logging
from typing import Any, cast

import httpx

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard, guarded_client
from progdata_crawler.core.find.brave import plain_text
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.find import FindRequest, SearchHit, SearchSource

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0

_CATEGORIES = {
    SearchSource.WEB: "general",
    SearchSource.NEWS: "news",
    SearchSource.IMAGES: "images",
}


def _hit(item: dict[str, Any], source: SearchSource) -> SearchHit | None:
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    published = item.get("publishedDate")
    image = item.get("img_src")
    return SearchHit(
        url=url,
        title=plain_text(item.get("title")) or url,
        snippet=plain_text(item.get("content")),
        source=source,
        published=published if isinstance(published, str) and published else None,
        image_url=image if source is SearchSource.IMAGES and isinstance(image, str) else None,
    )


class SearxngSearch:
    name = "searxng"

    def __init__(
        self,
        base_url: str | None,
        *,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        # The operator chose this address, so internal addresses are allowed here.
        self._client = guarded_client(
            AddressGuard(allow_private=True),
            user_agent=user_agent,
            timeout_s=TIMEOUT_S,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def search(self, request: FindRequest, source: SearchSource) -> list[SearchHit]:
        if self._base_url is None:
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "search is not configured: set PDC_SEARXNG_URL",
                provider=self.name,
            )
        params: dict[str, str] = {
            "q": request.query,
            "format": "json",
            "categories": _CATEGORIES[source],
            "pageno": "1",
        }
        if request.time_range is not None:
            params["time_range"] = request.time_range.value
        if request.language is not None:
            params["language"] = (
                f"{request.language}-{request.country}" if request.country else request.language
            )
        try:
            response = await self._client.get(self._base_url + "/search", params=params)
        except httpx.TimeoutException as error:
            raise CrawlerError(
                ErrorCode.FETCH_TIMEOUT, "the search provider did not answer in time"
            ) from error
        except httpx.HTTPError as error:
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "the search provider could not be reached",
                provider=self.name,
            ) from error
        if response.status_code == 429:
            raise CrawlerError(
                ErrorCode.RATE_LIMITED,
                "the search provider's limit was reached",
                provider=self.name,
            )
        if response.status_code == 403:
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "SearXNG refused the request: is the JSON format enabled in its settings?",
                provider=self.name,
            )
        if not response.is_success:
            log.warning(
                "search provider error",
                extra={"provider": self.name, "status": response.status_code},
            )
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "the search provider answered with an error",
                provider=self.name,
                status=response.status_code,
            )
        try:
            data: Any = response.json()
        except ValueError as error:
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE, "the search provider sent an unreadable answer"
            ) from error
        if not isinstance(data, dict):
            return []
        results: Any = cast(dict[str, Any], data).get("results")
        if not isinstance(results, list):
            return []
        items = [cast(dict[str, Any], i) for i in cast(list[Any], results) if isinstance(i, dict)]
        hits = [_hit(item, source) for item in items]
        return [hit for hit in hits if hit is not None][: request.limit]

    async def aclose(self) -> None:
        await self._client.aclose()
