"""Loading pages in a real browser (Chromium through Playwright).

Used only when a page needs JavaScript or the request needs browser features. One
browser process is shared. Browser contexts (the equivalent of separate browser
profiles) are kept per device and locale and reused; cookies are cleared between
pages so one request never sees another's state.

We behave like any other headless browser: no fingerprint spoofing, no hidden
automation flags, and the same honest User-Agent as plain HTTP requests.
"""

import asyncio
import contextlib
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin

from playwright.async_api import Browser, BrowserContext, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.fetch.http import HTML_TYPES, MAX_REDIRECTS
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.options import Device

NETWORK_IDLE_WAIT_MS = 2_000
"""After load, wait up to this long for the network to go quiet (for apps that fetch data)."""

MAX_IDLE_CONTEXTS = 2
"""Idle contexts kept per device and locale."""

DESKTOP: dict[str, Any] = {
    "viewport": {"width": 1280, "height": 800},
    "device_scale_factor": 1,
    "is_mobile": False,
    "has_touch": False,
}
MOBILE: dict[str, Any] = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}


@dataclass(frozen=True)
class RenderedPage:
    url: str
    """The final URL, after redirects."""
    status: int | None
    """HTTP status of the main document. None when the browser did not report one."""
    html: str
    """The DOM after rendering."""
    content_type: str
    screenshot: bytes | None
    """PNG, when requested."""
    elapsed_ms: int


def mobile_user_agent(user_agent: str) -> str:
    """The same honest User-Agent, marked as mobile."""
    if user_agent.endswith(")"):
        return user_agent[:-1] + "; mobile)"
    return user_agent + " (mobile)"


class BrowserPool:
    """A shared browser with reusable contexts. Use as an async context manager."""

    def __init__(
        self,
        user_agent: str,
        *,
        max_pages: int = 4,
        executable_path: Path | None = None,
        guard: AddressGuard | None = None,
    ) -> None:
        """With a `guard` that does not allow private networks, every request the browser
        makes (the page, its redirects and everything it loads) is checked."""
        self._guard = guard
        self._user_agent = user_agent
        self._executable_path = executable_path
        self._pages = asyncio.Semaphore(max_pages)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()
        self._idle: dict[tuple[Device, str | None], list[BrowserContext]] = {}
        self.contexts_created = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _ensure_browser(self) -> Browser:
        """Start the browser on first use, so a pool that is never used costs nothing."""
        async with self._start_lock:
            if self._browser is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    executable_path=str(self._executable_path) if self._executable_path else None,
                )
            return self._browser

    async def aclose(self) -> None:
        for contexts in self._idle.values():
            for context in contexts:
                await context.close()
        self._idle.clear()
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _take_context(self, device: Device, locale: str | None) -> BrowserContext:
        idle = self._idle.get((device, locale))
        if idle:
            return idle.pop()
        browser = await self._ensure_browser()
        profile = MOBILE if device is Device.MOBILE else DESKTOP
        user_agent = (
            mobile_user_agent(self._user_agent) if device is Device.MOBILE else self._user_agent
        )
        context = await browser.new_context(
            **profile,
            user_agent=user_agent,
            locale=locale,
            accept_downloads=False,
            extra_http_headers={"Accept-Language": locale} if locale else None,
        )
        if self._guard is not None and not self._guard.allow_private:
            await context.route("**/*", self._check_request)
        self.contexts_created += 1
        return context

    async def _check_request(self, route: Route) -> None:
        """Check every request the browser makes, including each step of a redirect chain.

        The browser follows redirects on its own without passing them through this
        handler, so the chain is followed here, one checked step at a time. The browser
        then gets either the final response or a single redirect to a URL that has
        already been checked.
        """
        assert self._guard is not None
        requested = url = route.request.url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                await self._guard.check_url(url)
                response = await route.fetch(url=url, max_redirects=0)
                location = response.headers.get("location")
                if not (300 <= response.status < 400 and location):
                    break
                url = urljoin(url, location)
            else:
                await route.abort("failed")
                return
        except CrawlerError:
            await route.abort("blockedbyclient")
            return
        except PlaywrightError:
            await route.abort("failed")
            return
        if url != requested:
            await route.fulfill(status=302, headers={"location": url})
        else:
            await route.fulfill(response=response)

    async def _return_context(
        self, context: BrowserContext, device: Device, locale: str | None
    ) -> None:
        idle = self._idle.setdefault((device, locale), [])
        if len(idle) >= MAX_IDLE_CONTEXTS:
            await context.close()
            return
        await context.clear_cookies()
        idle.append(context)

    async def render(
        self,
        url: str,
        *,
        timeout_ms: int = 30_000,
        wait_ms: int = 0,
        device: Device = Device.DESKTOP,
        locale: str | None = None,
        screenshot: bool = False,
        full_page: bool = False,
        accept_types: Collection[str] | None = HTML_TYPES,
    ) -> RenderedPage:
        """Load `url`, let it run, and return the rendered DOM.

        `timeout_ms` covers everything, including `wait_ms`. Raises `fetch_timeout`,
        `fetch_failed` or `unsupported_type`, like the HTTP fetcher.
        """
        started = time.monotonic()
        async with self._pages:
            context = await self._take_context(device, locale)
            healthy = True
            try:
                async with asyncio.timeout(timeout_ms / 1000):
                    return await self._load(
                        context, url, wait_ms, screenshot, full_page, accept_types, started
                    )
            except TimeoutError as exc:
                healthy = False
                raise CrawlerError(
                    ErrorCode.FETCH_TIMEOUT,
                    f"the page did not finish loading within {timeout_ms} ms",
                    url=url,
                ) from exc
            except PlaywrightTimeoutError as exc:
                raise CrawlerError(
                    ErrorCode.FETCH_TIMEOUT,
                    f"the page did not finish loading within {timeout_ms} ms",
                    url=url,
                ) from exc
            except PlaywrightError as exc:
                if "ERR_BLOCKED_BY_CLIENT" in exc.message:
                    raise CrawlerError(
                        ErrorCode.ADDRESS_NOT_ALLOWED,
                        "requests to internal network addresses are not allowed",
                        url=url,
                    ) from exc
                raise CrawlerError(
                    ErrorCode.FETCH_FAILED,
                    f"the browser could not load the page: {exc.message.splitlines()[0]}",
                    url=url,
                ) from exc
            finally:
                if healthy:
                    await self._return_context(context, device, locale)
                else:
                    await context.close()

    async def _load(
        self,
        context: BrowserContext,
        url: str,
        wait_ms: int,
        screenshot: bool,
        full_page: bool,
        accept_types: Collection[str] | None,
        started: float,
    ) -> RenderedPage:
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="load")
            content_type = ""
            if response is not None:
                header = await response.header_value("content-type") or ""
                content_type = header.split(";")[0].strip().lower()
            if accept_types is not None and content_type and content_type not in accept_types:
                raise CrawlerError(
                    ErrorCode.UNSUPPORTED_TYPE,
                    f"content type {content_type!r} is not supported",
                    url=page.url,
                    content_type=content_type,
                )
            # Pages that keep polling never go idle; what we have then is good enough.
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_WAIT_MS)
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            image = await page.screenshot(full_page=full_page, type="png") if screenshot else None
            return RenderedPage(
                url=page.url,
                status=response.status if response is not None else None,
                html=await page.content(),
                content_type=content_type or "text/html",
                screenshot=image,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        finally:
            await page.close()
