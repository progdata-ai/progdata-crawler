"""Page metadata from the HTML head."""

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from progdata_crawler.core.extract.links import absolute_url
from progdata_crawler.models.page import PageMetadata

_WHITESPACE = re.compile(r"\s+")

OPEN_GRAPH_URL_KEYS = frozenset({"url", "image", "image:url", "image:secure_url", "video", "audio"})


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = _WHITESPACE.sub(" ", value).strip()
    return value or None


def extract_metadata(
    html: str,
    base_url: str,
    *,
    status: int | None = None,
    content_type: str | None = None,
) -> PageMetadata:
    tree = LexborHTMLParser(html)

    named: dict[str, str] = {}
    properties: dict[str, str] = {}
    for node in tree.css("meta[content]"):
        attrs = node.attributes
        content = _clean(attrs.get("content"))
        if content is None:
            continue
        if attrs.get("name"):
            named.setdefault((attrs.get("name") or "").lower(), content)
        if attrs.get("property"):
            properties.setdefault((attrs.get("property") or "").lower(), content)

    open_graph: dict[str, str] = {}
    for key, value in properties.items():
        if not key.startswith("og:"):
            continue
        short = key.removeprefix("og:")
        if short in OPEN_GRAPH_URL_KEYS:
            value = absolute_url(base_url, value) or value
        open_graph.setdefault(short, value)

    title_node = tree.css_first("title")
    canonical_node = tree.css_first("link[rel~=canonical][href]")
    html_node = tree.css_first("html")
    keywords = named.get("keywords")

    return PageMetadata(
        title=_clean(title_node.text()) if title_node is not None else open_graph.get("title"),
        description=named.get("description") or open_graph.get("description"),
        language=_clean(html_node.attributes.get("lang")) if html_node is not None else None,
        canonical_url=(
            urljoin(base_url, canonical_node.attributes.get("href") or "")
            if canonical_node is not None
            else None
        ),
        author=named.get("author"),
        keywords=tuple(k.strip() for k in keywords.split(",") if k.strip()) if keywords else (),
        published_time=properties.get("article:published_time"),
        open_graph=open_graph,
        status=status,
        content_type=content_type,
    )
