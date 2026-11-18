"""robots.txt: decide whether we may fetch a URL, and how slowly.

Rules follow RFC 9309:

- A robots.txt that returns 4xx (other than 429) means there are no rules: everything
  is allowed.
- A robots.txt that cannot be fetched (5xx, 429, network error) means the site is
  unavailable: everything is disallowed until we try again.
- A robots.txt that cannot be parsed is read as leniently as possible.

robots.txt is fetched once per origin (scheme, host and port) and cached. It applies to
every request we make to a site, without exceptions: there is no allowlist.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from protego import Protego

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import ErrorCode

AGENT_TOKEN = "progdata-crawler"
"""The product token sites can name in robots.txt (`User-agent: progdata-crawler`)."""

DEFAULT_TTL_S = 3600.0
"""How long a fetched robots.txt is trusted."""

UNAVAILABLE_TTL_S = 60.0
"""How long we wait before retrying a robots.txt that could not be fetched."""


@dataclass(frozen=True)
class RobotsFile:
    """The response to a robots.txt request."""

    status: int
    text: str


RobotsFetcher = Callable[[str], Awaitable[RobotsFile]]
"""Fetches a robots.txt URL. Raises on network errors. Follows redirects."""


@dataclass(frozen=True)
class RobotsRules:
    """The rules that apply to us on one origin."""

    _parser: Protego | None
    """None means one rule for everything, given by `_allow_all`."""
    _allow_all: bool

    @classmethod
    def allow_all(cls) -> "RobotsRules":
        return cls(None, True)

    @classmethod
    def disallow_all(cls) -> "RobotsRules":
        return cls(None, False)

    @classmethod
    def parse(cls, text: str) -> "RobotsRules":
        return cls(Protego.parse(text), True)

    def can_fetch(self, url: str) -> bool:
        if self._parser is None:
            return self._allow_all
        return bool(self._parser.can_fetch(url, AGENT_TOKEN))

    def crawl_delay(self) -> float | None:
        """Seconds to wait between requests, if robots.txt sets it for us."""
        if self._parser is None:
            return None
        delay: float | None = self._parser.crawl_delay(AGENT_TOKEN)
        return float(delay) if delay is not None else None

    def sitemaps(self) -> list[str]:
        if self._parser is None:
            return []
        return [str(url) for url in self._parser.sitemaps]


def origin_of(url: str) -> str:
    """`scheme://host[:port]` in lowercase, the unit robots.txt applies to."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {url!r}")
    netloc = parts.hostname.lower()
    if parts.port is not None:
        netloc += f":{parts.port}"
    return f"{parts.scheme.lower()}://{netloc}"


@dataclass
class _Entry:
    rules: RobotsRules
    expires_at: float


class RobotsPolicy:
    """Answers "may we fetch this URL?" with cached robots.txt rules per origin."""

    def __init__(
        self,
        fetcher: RobotsFetcher,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        unavailable_ttl_s: float = UNAVAILABLE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """robots.txt applies to every URL, without exceptions."""
        self._fetcher = fetcher
        self._ttl_s = ttl_s
        self._unavailable_ttl_s = unavailable_ttl_s
        self._clock = clock
        self._cache: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def rules_for(self, url: str) -> RobotsRules:
        origin = origin_of(url)
        entry = self._cache.get(origin)
        if entry is not None and entry.expires_at > self._clock():
            return entry.rules
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            entry = self._cache.get(origin)
            if entry is not None and entry.expires_at > self._clock():
                return entry.rules
            rules, ttl = await self._load(origin)
            self._cache[origin] = _Entry(rules, self._clock() + ttl)
            return rules

    async def check(self, url: str) -> None:
        """Raise `robots_disallowed` unless we may fetch `url`."""
        rules = await self.rules_for(url)
        if not rules.can_fetch(url):
            raise CrawlerError(
                ErrorCode.ROBOTS_DISALLOWED,
                "robots.txt does not allow fetching this URL",
                url=url,
            )

    async def crawl_delay(self, url: str) -> float | None:
        return (await self.rules_for(url)).crawl_delay()

    async def _load(self, origin: str) -> tuple[RobotsRules, float]:
        try:
            response = await self._fetcher(origin + "/robots.txt")
        except Exception:
            return RobotsRules.disallow_all(), self._unavailable_ttl_s
        if 200 <= response.status < 300:
            return RobotsRules.parse(response.text), self._ttl_s
        if 400 <= response.status < 500 and response.status != 429:
            return RobotsRules.allow_all(), self._ttl_s
        return RobotsRules.disallow_all(), self._unavailable_ttl_s
