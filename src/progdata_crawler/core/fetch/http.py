"""Fetching pages over HTTP.

`HttpFetcher` wraps one shared httpx client. It always sends our honest User-Agent,
enforces a total time limit and a size limit, and checks the content type before it
downloads the body. Failures become `CrawlerError`s with stable codes.
"""

import asyncio
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import httpx

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard, GuardedTransport
from progdata_crawler.core.polite.robots import RobotsFile
from progdata_crawler.models.envelope import ErrorCode

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
"""Content types the page pipeline can handle."""

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
ROBOTS_MAX_BYTES = 512 * 1024
"""RFC 9309 asks crawlers to read at least 500 KiB of robots.txt."""

MAX_REDIRECTS = 10

BLOCKED_STATUSES = frozenset({401, 403, 451})
"""Statuses that mean the site refuses to serve us. We do not retry around them."""


@dataclass(frozen=True)
class HttpResponse:
    url: str
    """The final URL, after redirects."""
    status: int
    headers: Mapping[str, str]
    """Header names in lowercase."""
    content: bytes
    content_type: str
    """Media type in lowercase, without parameters. Empty if the server sent none."""
    charset: str | None
    elapsed_ms: int
    redirects: tuple[str, ...] = field(default=())
    """URLs we were redirected from, in order."""

    def text(self) -> str:
        """The body decoded with the declared charset, falling back to UTF-8."""
        try:
            return self.content.decode(self.charset or "utf-8", errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _media_type(header: str | None) -> tuple[str, str | None]:
    if not header:
        return "", None
    media_type, _, params = header.partition(";")
    charset = None
    for param in params.split(";"):
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip().strip('"').lower()
    return media_type.strip().lower(), charset


def raise_for_blocked(response: HttpResponse) -> None:
    """Raise `blocked_by_site` when the site refused to serve us."""
    if response.status in BLOCKED_STATUSES:
        raise CrawlerError(
            ErrorCode.BLOCKED_BY_SITE,
            "the site refused to serve this page to our crawler",
            url=response.url,
            status=response.status,
        )


class HttpFetcher:
    """Fetches pages with one shared HTTP client. Use as an async context manager."""

    def __init__(
        self,
        user_agent: str,
        *,
        timeout_ms: int = 30_000,
        max_bytes: int = DEFAULT_MAX_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        guard: AddressGuard | None = None,
    ) -> None:
        """With a `guard` that does not allow private networks, every connection,
        including each redirect, is checked against internal addresses."""
        self._timeout_ms = timeout_ms
        self._max_bytes = max_bytes
        guarded = guard is not None and not guard.allow_private
        if guarded and transport is None:
            assert guard is not None
            transport = GuardedTransport(guard)
        self._client = httpx.AsyncClient(
            http2=transport is None,
            transport=transport,
            trust_env=not guarded,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout_ms / 1000),
        )

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
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        timeout_ms: int | None = None,
        locale: str | None = None,
        accept_types: Collection[str] | None = HTML_TYPES,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        """GET `url`.

        Raises `unsupported_type` when the content type is not in `accept_types` (None
        accepts anything) or the body is larger than `max_bytes`, `fetch_timeout` when the
        whole request takes longer than `timeout_ms`, and `fetch_failed` on network errors.
        Error statuses are returned, not raised; see `raise_for_blocked`.
        """
        timeout_s = (timeout_ms or self._timeout_ms) / 1000
        limit = max_bytes or self._max_bytes
        headers = {"Accept-Language": locale} if locale else {}
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                return await self._get(url, headers, accept_types, limit, started)
        except TimeoutError as exc:
            raise CrawlerError(
                ErrorCode.FETCH_TIMEOUT,
                f"the page did not respond within {round(timeout_s * 1000)} ms",
                url=url,
            ) from exc
        except httpx.TimeoutException as exc:
            raise CrawlerError(
                ErrorCode.FETCH_TIMEOUT,
                f"the page did not respond within {round(timeout_s * 1000)} ms",
                url=url,
            ) from exc
        except httpx.TooManyRedirects as exc:
            raise CrawlerError(ErrorCode.FETCH_FAILED, "too many redirects", url=url) from exc
        except httpx.HTTPError as exc:
            raise CrawlerError(
                ErrorCode.FETCH_FAILED,
                f"the page could not be fetched: {type(exc).__name__}",
                url=url,
            ) from exc

    async def _get(
        self,
        url: str,
        headers: dict[str, str],
        accept_types: Collection[str] | None,
        limit: int,
        started: float,
    ) -> HttpResponse:
        async with self._client.stream("GET", url, headers=headers) as response:
            content_type, charset = _media_type(response.headers.get("content-type"))
            final_url = str(response.url)
            if accept_types is not None and content_type not in accept_types:
                raise CrawlerError(
                    ErrorCode.UNSUPPORTED_TYPE,
                    f"content type {content_type or 'unknown'!r} is not supported",
                    url=final_url,
                    content_type=content_type,
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise self._too_large(final_url, limit)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > limit:
                    raise self._too_large(final_url, limit)
            return HttpResponse(
                url=final_url,
                status=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                content=bytes(body),
                content_type=content_type,
                charset=charset or response.charset_encoding,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                redirects=tuple(str(r.url) for r in response.history),
            )

    @staticmethod
    def _too_large(url: str, limit: int) -> CrawlerError:
        return CrawlerError(
            ErrorCode.UNSUPPORTED_TYPE,
            f"the page is larger than {limit} bytes",
            url=url,
            max_bytes=limit,
        )

    async def fetch_robots(self, url: str) -> RobotsFile:
        """Fetch a robots.txt file for `RobotsPolicy`. Network errors propagate."""
        response = await self.fetch(url, accept_types=None, max_bytes=ROBOTS_MAX_BYTES)
        return RobotsFile(response.status, response.text())
