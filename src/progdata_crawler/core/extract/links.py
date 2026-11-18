"""Links on a page."""

import re
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from progdata_crawler.models.page import Link

SKIPPED_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "sms:", "ftp:")

_WHITESPACE = re.compile(r"\s+")


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def absolute_url(base_url: str, href: str) -> str | None:
    """Resolve `href` against `base_url` and drop the fragment. None for non-web links."""
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith(SKIPPED_SCHEMES):
        return None
    url, _ = urldefrag(urljoin(base_url, href))
    if urlsplit(url).scheme not in ("http", "https"):
        return None
    return url


def extract_links(html: str, base_url: str) -> tuple[Link, ...]:
    """Every distinct http(s) link in the page, in order of first appearance.

    `<base href>` is honoured. Fragment-only links, javascript:, mailto: and similar are
    left out.
    """
    tree = LexborHTMLParser(html)
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        base_url = urljoin(base_url, base_node.attributes.get("href") or "")
    page_host = _host(base_url)

    links: dict[str, Link] = {}
    for anchor in tree.css("a[href]"):
        url = absolute_url(base_url, anchor.attributes.get("href") or "")
        if url is None or url in links:
            continue
        text = _WHITESPACE.sub(" ", anchor.text(separator=" ")).strip()
        if not text:
            text = (
                anchor.attributes.get("title") or anchor.attributes.get("aria-label") or ""
            ).strip()
        rel = tuple((anchor.attributes.get("rel") or "").lower().split())
        links[url] = Link(url=url, text=text, internal=_host(url) == page_host, rel=rel)
    return tuple(links.values())
