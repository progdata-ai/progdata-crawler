"""The Brave Search API: the default search provider.

Brave runs its own index and an official API, so we search through an agreement
instead of scraping a search engine. The API key is a secret: it is sent only to
the API and never logged.
"""

import html
import logging
import re
from typing import Any, cast

import httpx

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard, guarded_client
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.find import FindRequest, SearchHit, SearchSource, TimeRange

log = logging.getLogger(__name__)

API_URL = "https://api.search.brave.com/res/v1"
TIMEOUT_S = 15.0

_ENDPOINTS = {
    SearchSource.WEB: "/web/search",
    SearchSource.NEWS: "/news/search",
    SearchSource.IMAGES: "/images/search",
}
_FRESHNESS = {
    TimeRange.DAY: "pd",
    TimeRange.WEEK: "pw",
    TimeRange.MONTH: "pm",
    TimeRange.YEAR: "py",
}
_TAG = re.compile(r"<[^>]+>")


def plain_text(value: object) -> str:
    """Brave marks matches with HTML tags; hits carry plain text."""
    if not isinstance(value, str):
        return ""
    return html.unescape(_TAG.sub("", value)).strip()


def _results(data: dict[str, Any], source: SearchSource) -> list[dict[str, Any]]:
    container: Any = data.get("web") if source is SearchSource.WEB else data
    if not isinstance(container, dict):
        return []
    results = cast(dict[str, Any], container).get("results")
    if not isinstance(results, list):
        return []
    return [
        cast(dict[str, Any], item) for item in cast(list[Any], results) if isinstance(item, dict)
    ]


def _hit(item: dict[str, Any], source: SearchSource) -> SearchHit | None:
    url = item.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    image_url: str | None = None
    if source is SearchSource.IMAGES:
        properties = item.get("properties")
        if isinstance(properties, dict):
            found = cast(dict[str, Any], properties).get("url")
            image_url = found if isinstance(found, str) else None
    published = item.get("page_age") or item.get("age")
    return SearchHit(
        url=url,
        title=plain_text(item.get("title")) or url,
        snippet=plain_text(item.get("description")),
        source=source,
        published=published if isinstance(published, str) else None,
        image_url=image_url,
    )


def _retry_after(response: httpx.Response) -> float | None:
    for header in ("retry-after", "x-ratelimit-reset"):
        value = response.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            try:
                return float(first)
            except ValueError:
                continue
    return None


class BraveSearch:
    name = "brave"

    def __init__(
        self,
        api_key: str | None,
        *,
        user_agent: str,
        guard: AddressGuard,
        transport: httpx.AsyncBaseTransport | None = None,
        api_url: str = API_URL,
    ) -> None:
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._client = guarded_client(
            guard,
            user_agent=user_agent,
            timeout_s=TIMEOUT_S,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def search(self, request: FindRequest, source: SearchSource) -> list[SearchHit]:
        if not self._api_key:
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "search is not configured: set PDC_BRAVE_API_KEY",
                provider=self.name,
            )
        params: dict[str, str | int] = {"q": request.query, "count": request.limit}
        if request.time_range is not None:
            params["freshness"] = _FRESHNESS[request.time_range]
        if request.country is not None:
            params["country"] = request.country.lower()
        if request.language is not None:
            params["search_lang"] = request.language
        if source is SearchSource.WEB:
            params["result_filter"] = "web"
        try:
            response = await self._client.get(
                self._api_url + _ENDPOINTS[source],
                params=params,
                headers={"X-Subscription-Token": self._api_key},
            )
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
            details: dict[str, Any] = {"provider": self.name}
            retry_after = _retry_after(response)
            if retry_after is not None:
                details["retry_after_s"] = retry_after
            raise CrawlerError(
                ErrorCode.RATE_LIMITED, "the search provider's limit was reached", **details
            )
        if response.status_code in (401, 403):
            raise CrawlerError(
                ErrorCode.SEARCH_UNAVAILABLE,
                "the search provider refused the API key",
                provider=self.name,
                status=response.status_code,
            )
        if response.status_code == 422:
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                "the search provider refused the query",
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
        hits = [_hit(item, source) for item in _results(cast(dict[str, Any], data), source)]
        return [hit for hit in hits if hit is not None][: request.limit]

    async def aclose(self) -> None:
        await self._client.aclose()
