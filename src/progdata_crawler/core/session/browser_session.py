"""Browser sessions: a page kept open between calls, so an agent can take a step, look
at the result and decide the next.

Each session has a browser context of its own: cookies and storage never leak between
sessions and are gone when it closes. Sessions close after `PDC_SESSION_TTL_S` without
use, after `PDC_SESSION_MAX_AGE_S` in any case, and when the service stops. At most
`PDC_MAX_SESSIONS` are open at once. Every navigation in a session goes through the
same robots.txt, rate limit and address checks as any other request.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.browser import BrowserSession
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.options import PageOptions
from progdata_crawler.models.session import SessionOpenRequest, SessionStepRequest, SessionView

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 5.0


@dataclass
class _Entry:
    browser: BrowserSession
    options: PageOptions
    opened_at: float
    used_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionManager:
    """Use as an async context manager; the sweeper that closes idle sessions runs while
    it is open."""

    def __init__(
        self,
        crawler: Crawler,
        *,
        ttl_s: float | None = None,
        max_age_s: float | None = None,
        max_sessions: int | None = None,
        clock: Callable[[], float] = time.time,
        sweep_interval_s: float = SWEEP_INTERVAL_S,
    ) -> None:
        settings = crawler.settings
        self._crawler = crawler
        self._ttl_s = ttl_s if ttl_s is not None else settings.session_ttl_s
        self._max_age_s = max_age_s if max_age_s is not None else settings.session_max_age_s
        self._max_sessions = max_sessions if max_sessions is not None else settings.max_sessions
        self._clock = clock
        self._sweep_interval_s = sweep_interval_s
        self._sessions: dict[str, _Entry] = {}
        self._opening = 0
        self._sweeper: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._sweeper = asyncio.create_task(self._sweep_forever(), name="session-sweeper")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def open_count(self) -> int:
        return len(self._sessions)

    def _expires_at(self, entry: _Entry) -> float:
        return min(entry.used_at + self._ttl_s, entry.opened_at + self._max_age_s)

    async def open(self, request: SessionOpenRequest) -> SessionView:
        """Raises `rate_limited` when the maximum number of sessions is open."""
        if len(self._sessions) + self._opening >= self._max_sessions:
            raise CrawlerError(
                ErrorCode.RATE_LIMITED,
                f"{self._max_sessions} browser sessions are open; close one or wait",
                retry_after_s=self._ttl_s,
            )
        options = PageOptions.model_validate(request.model_dump(exclude={"url"}))
        self._opening += 1
        try:
            browser, page = await self._crawler.open_session(str(request.url), options)
        finally:
            self._opening -= 1
        now = self._clock()
        session_id = secrets.token_urlsafe(16)
        entry = _Entry(browser, options.model_copy(update={"steps": ()}), now, now)
        self._sessions[session_id] = entry
        log.info("session opened", extra={"session_id": session_id})
        return SessionView(id=session_id, expires_at=self._expires_at(entry), page=page)

    async def step(self, session_id: str, request: SessionStepRequest) -> SessionView:
        """Raises `not_found` for unknown, closed or expired sessions."""
        entry = self._live(session_id)
        async with entry.lock:  # one step at a time per session
            entry = self._live(session_id)
            options = entry.options.model_copy(update={"steps": request.steps})
            page = await self._crawler.session_step(entry.browser, options)
            entry.used_at = self._clock()
            return SessionView(id=session_id, expires_at=self._expires_at(entry), page=page)

    async def close(self, session_id: str) -> SessionView:
        """Raises `not_found` for unknown or already closed sessions."""
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            raise _not_found(session_id)
        await self._crawler.close_session(entry.browser)
        log.info("session closed", extra={"session_id": session_id, "reason": "closed"})
        return SessionView(id=session_id, expires_at=self._clock(), closed=True)

    def _live(self, session_id: str) -> _Entry:
        entry = self._sessions.get(session_id)
        if entry is None or self._expires_at(entry) <= self._clock():
            raise _not_found(session_id)
        return entry

    async def sweep(self) -> int:
        """Close expired sessions now. Returns how many were closed."""
        now = self._clock()
        expired = [
            sid
            for sid, entry in self._sessions.items()
            if self._expires_at(entry) <= now and not entry.lock.locked()  # not mid-step
        ]
        for session_id in expired:
            entry = self._sessions.pop(session_id)
            await self._crawler.close_session(entry.browser)
            log.info("session closed", extra={"session_id": session_id, "reason": "expired"})
        return len(expired)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval_s)
            try:
                await self.sweep()
            except Exception:
                log.exception("session sweep failed")

    async def aclose(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        for session_id in list(self._sessions):
            entry = self._sessions.pop(session_id)
            await self._crawler.close_session(entry.browser)


def _not_found(session_id: str) -> CrawlerError:
    return CrawlerError(
        ErrorCode.NOT_FOUND,
        "no open session with this id (it may have expired)",
        session_id=session_id,
    )
