"""Deciding when a page needs a browser.

A browser is slow and heavy, so we use one only when the HTML we got over plain HTTP is
an empty shell that JavaScript fills in, or when the request needs browser features
(a screenshot, or waiting after load).
"""

import re

from selectolax.lexbor import LexborHTMLParser

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import ErrorCode
from progdata_crawler.models.options import Output, PageOptions, RenderMode

ENOUGH_TEXT = 200
"""A page with at least this many characters of visible text is not an empty shell."""

LITTLE_TEXT = 50
"""Below this, a page that loads scripts is treated as a shell even without a mount point."""

MOUNT_POINT_SELECTORS = (
    "#app",
    "#root",
    "#__next",
    "#__nuxt",
    "#___gatsby",
    "#svelte",
    "[data-reactroot]",
    "[ng-version]",
    "[ng-app]",
)
"""Elements that client-side frameworks render into."""

_WHITESPACE = re.compile(r"\s+")
_INVISIBLE = "script, style, noscript, template"


def _visible_text(tree: LexborHTMLParser) -> str:
    body = tree.body
    if body is None:
        return ""
    for node in body.css(_INVISIBLE):
        node.decompose()
    return _WHITESPACE.sub(" ", body.text(separator=" ")).strip()


def looks_like_js_shell(html: str) -> bool:
    """True when the HTML has little visible content and signs that JavaScript adds it."""
    tree = LexborHTMLParser(html)
    has_scripts = bool(tree.css("script"))
    noscript_asks_for_js = any("javascript" in node.text().lower() for node in tree.css("noscript"))
    empty_mount_point = any(
        not _WHITESPACE.sub("", node.text())
        for selector in MOUNT_POINT_SELECTORS
        for node in tree.css(selector)
    )

    text = _visible_text(tree)
    if len(text) >= ENOUGH_TEXT:
        return False
    if empty_mount_point or noscript_asks_for_js:
        return True
    return has_scripts and len(text) < LITTLE_TEXT


def needs_browser_features(options: PageOptions) -> bool:
    """True when the request asks for something only a browser can do."""
    return Output.SCREENSHOT in options.outputs or options.wait_ms > 0 or bool(options.steps)


def check_render_options(options: PageOptions) -> None:
    """Reject requests that need a browser but forbid one."""
    if options.render is RenderMode.NEVER and needs_browser_features(options):
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            "screenshot, wait_ms and steps need a browser, but render is 'never'",
        )


def should_render_first(options: PageOptions) -> bool:
    """True when we should skip plain HTTP and go straight to the browser."""
    return options.render is RenderMode.ALWAYS or needs_browser_features(options)


def should_render_after(options: PageOptions, html: str) -> bool:
    """After a plain HTTP fetch: True when the page should be loaded in a browser."""
    if options.render is RenderMode.NEVER:
        return False
    return should_render_first(options) or looks_like_js_shell(html)
