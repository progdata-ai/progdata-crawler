"""Per-domain rate limiting.

Requests to the same host are spaced out: at least `min_interval_s` apart, or the
Crawl-delay from robots.txt if that is longer. When a site answers 429 or 503 we back
off, using its Retry-After header when it sends one and exponential backoff otherwise.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import ErrorCode

DEFAULT_MIN_INTERVAL_S = 1.0
"""Default spacing between requests to the same host."""

BACKOFF_BASE_S = 2.0
BACKOFF_MAX_S = 300.0
"""Backoff without Retry-After: 2, 4, 8 ... seconds, at most five minutes."""

BACKOFF_STATUSES = frozenset({429, 503})


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait from a Retry-After header: delay-seconds or an HTTP date.

    `now` is the current Unix time. Returns None when the header is missing or invalid.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - now)


def host_key(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    return host.lower()


@dataclass
class _HostState:
    next_at: float = 0.0
    failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DomainRateLimiter:
    """Spaces out requests per host and backs off when a host asks us to."""

    def __init__(
        self,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval_s = min_interval_s
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._hosts: dict[str, _HostState] = {}

    def _state(self, url: str) -> _HostState:
        return self._hosts.setdefault(host_key(url), _HostState())

    async def acquire(
        self,
        url: str,
        *,
        crawl_delay: float | None = None,
        max_wait_s: float | None = None,
    ) -> None:
        """Wait until we may send a request to the host of `url`.

        Raises `rate_limited` instead of waiting when the wait would be longer than
        `max_wait_s`, for example because the host asked us to come back much later.
        """
        state = self._state(url)
        async with state.lock:
            wait = max(0.0, state.next_at - self._clock())
            if max_wait_s is not None and wait > max_wait_s:
                raise CrawlerError(
                    ErrorCode.RATE_LIMITED,
                    "the site asked us to slow down; try again later",
                    url=url,
                    retry_after_s=round(wait, 3),
                )
            if wait > 0:
                await self._sleep(wait)
            interval = max(self._min_interval_s, crawl_delay or 0.0)
            state.next_at = self._clock() + interval

    def record_response(self, url: str, status: int, retry_after: str | None = None) -> float:
        """Tell the limiter how the host answered. Returns the backoff in seconds (0 if none)."""
        state = self._state(url)
        if status in BACKOFF_STATUSES:
            state.failures += 1
            delay = parse_retry_after(retry_after, self._wall_clock())
            if delay is None:
                delay = min(
                    self._backoff_base_s * 2 ** (state.failures - 1),
                    self._backoff_max_s,
                )
            state.next_at = max(state.next_at, self._clock() + delay)
            return delay
        if status < 400:
            state.failures = 0
        return 0.0
