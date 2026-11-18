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
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.fetch.http import HTML_TYPES, MAX_REDIRECTS
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.options import Device
from progdata_crawler.models.steps import (
    CaptureStep,
    ClickStep,
    PressStep,
    ScrollDirection,
    ScrollStep,
    Step,
    TypeStep,
    WaitStep,
)

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


NavigationCheck = Callable[[str], Awaitable[None]]
"""Called before the browser goes to a new page; raises `CrawlerError` to refuse."""

STEP_SETTLE_MS = 500
"""After a step, wait up to this long for the page to settle."""


@dataclass(frozen=True)
class RawCapture:
    step: int
    url: str
    html: str
    screenshot: bytes | None


def describe(step: Step) -> str:
    target = getattr(step, "selector", None) or getattr(step, "key", None)
    return f"{step.action} {target!r}" if target else step.action


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
    captures: tuple[RawCapture, ...] = ()


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
        navigation_check: NavigationCheck | None = None,
    ) -> None:
        """With a `guard` that does not allow private networks, every request the browser
        makes (the page, its redirects and everything it loads) is checked.
        `navigation_check` (robots.txt and the rate limit) is called for every page the
        browser goes to after the first: redirects, links clicked and forms sent."""
        self._guard = guard
        self._navigation_check = navigation_check
        self._prechecked: dict[Page, str] = {}
        self._refusals: dict[Page, CrawlerError] = {}
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
        if self._guarded or self._navigation_check is not None:
            await context.route("**/*", self._check_request)
        self.contexts_created += 1
        return context

    @property
    def _guarded(self) -> bool:
        return self._guard is not None and not self._guard.allow_private

    async def _check_request(self, route: Route) -> None:
        """Check requests the browser makes, including each step of a redirect chain.

        Every request goes through the address guard (when it refuses private networks).
        Navigations of the main frame also go through `navigation_check`. The browser
        follows redirects on its own without passing them through this handler, so the
        chain is followed here, one checked step at a time. The browser then gets either
        the final response or a single redirect to a URL that has already been checked.
        """
        request = route.request
        navigation = request.is_navigation_request() and request.frame.parent_frame is None
        page = request.frame.page
        if not self._guarded and not (navigation and self._navigation_check is not None):
            await route.continue_()
            return
        requested = url = request.url
        try:
            for hop in range(MAX_REDIRECTS + 1):
                if self._guarded:
                    assert self._guard is not None
                    await self._guard.check_url(url)
                if navigation and self._navigation_check is not None:
                    if self._prechecked.get(page) == url:
                        del self._prechecked[page]  # the crawler checked this one already
                    else:
                        await self._navigation_check(url)
                if hop == 0:
                    response = await route.fetch(max_redirects=0)
                else:
                    response = await route.fetch(url=url, method="GET", max_redirects=0)
                location = response.headers.get("location")
                if not (300 <= response.status < 400 and location):
                    break
                url = urljoin(url, location)
            else:
                await route.abort("failed")
                return
        except CrawlerError as error:
            if navigation:
                self._refusals[page] = error
            await route.abort(
                "accessdenied" if error.code is ErrorCode.ROBOTS_DISALLOWED else "blockedbyclient"
            )
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
        steps: tuple[Step, ...] = (),
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
                        context, url, wait_ms, screenshot, full_page, accept_types, started, steps
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
                if "ERR_ACCESS_DENIED" in exc.message:
                    raise CrawlerError(
                        ErrorCode.ROBOTS_DISALLOWED,
                        "robots.txt does not allow fetching this URL",
                        url=url,
                    ) from exc
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
        steps: tuple[Step, ...] = (),
    ) -> RenderedPage:
        page = await context.new_page()
        self._prechecked[page] = url
        try:
            response = await page.goto(url, wait_until="load")
            self._raise_refusal(page)
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
            captures: list[RawCapture] = []
            for number, step in enumerate(steps, start=1):
                capture = await self._run_step(page, number, step, screenshot, full_page)
                if capture is not None:
                    captures.append(capture)
            image = await page.screenshot(full_page=full_page, type="png") if screenshot else None
            return RenderedPage(
                url=page.url,
                status=response.status if response is not None else None,
                html=await page.content(),
                content_type=content_type or "text/html",
                screenshot=image,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                captures=tuple(captures),
            )
        finally:
            self._prechecked.pop(page, None)
            self._refusals.pop(page, None)
            await page.close()

    def _raise_refusal(self, page: Page) -> None:
        """A navigation the checks refused shows up as a browser error page; raise the
        reason instead."""
        refusal = self._refusals.pop(page, None)
        if refusal is not None:
            raise refusal

    async def _run_step(
        self, page: Page, number: int, step: Step, screenshot: bool, full_page: bool
    ) -> RawCapture | None:
        """Run one step. Raises `step_failed` naming the step when it cannot be done."""
        timeout = step.timeout_ms
        try:
            match step:
                case ClickStep():
                    await page.locator(step.selector).first.click(timeout=timeout)
                case TypeStep():
                    field = page.locator(step.selector).first
                    if step.clear:
                        await field.fill("", timeout=timeout)
                    await field.press_sequentially(step.text, timeout=timeout)
                case PressStep():
                    if step.selector is not None:
                        await page.locator(step.selector).first.press(step.key, timeout=timeout)
                    else:
                        await page.keyboard.press(step.key)
                case ScrollStep():
                    if step.selector is not None:
                        element = page.locator(step.selector).first
                        await element.scroll_into_view_if_needed(timeout=timeout)
                    else:
                        sign = 1 if step.direction is ScrollDirection.DOWN else -1
                        await page.mouse.wheel(0, sign * step.pixels)
                case WaitStep():
                    if step.selector is not None:
                        await page.locator(step.selector).first.wait_for(
                            state="visible", timeout=timeout
                        )
                    if step.ms:
                        await page.wait_for_timeout(step.ms)
                case CaptureStep():
                    image = (
                        await page.screenshot(full_page=full_page, type="png")
                        if screenshot
                        else None
                    )
                    return RawCapture(number, page.url, await page.content(), image)
        except PlaywrightTimeoutError as exc:
            raise CrawlerError(
                ErrorCode.STEP_FAILED,
                f"step {number} ({describe(step)}): no matching element within {timeout} ms",
                step=number,
            ) from exc
        except PlaywrightError as exc:
            self._raise_refusal(page)
            raise CrawlerError(
                ErrorCode.STEP_FAILED,
                f"step {number} ({describe(step)}): {exc.message.splitlines()[0]}",
                step=number,
            ) from exc
        # A step may start a navigation; let it finish, then report a refused one.
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("load", timeout=STEP_SETTLE_MS * 10)
            await page.wait_for_load_state("networkidle", timeout=STEP_SETTLE_MS)
        self._raise_refusal(page)
        return None
