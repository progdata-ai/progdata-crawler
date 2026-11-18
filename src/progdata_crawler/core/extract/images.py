"""Images on a page."""

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from progdata_crawler.core.extract.links import absolute_url
from progdata_crawler.models.page import Image

_WHITESPACE = re.compile(r"\s+")


def _dimension(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip().removesuffix("px")
    return int(value) if value.isdigit() else None


def _first_srcset_url(srcset: str) -> str | None:
    first = srcset.split(",")[0].strip()
    return first.split()[0] if first else None


def extract_images(html: str, base_url: str) -> tuple[Image, ...]:
    """Every distinct `<img>` with a usable source, in document order.

    Uses `src`, then `data-src` (lazy loading), then the first `srcset` entry.
    """
    tree = LexborHTMLParser(html)
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        base_url = urljoin(base_url, base_node.attributes.get("href") or "")

    images: dict[str, Image] = {}
    for node in tree.css("img"):
        attrs = node.attributes
        source = attrs.get("src") or attrs.get("data-src")
        if not source and attrs.get("srcset"):
            source = _first_srcset_url(attrs.get("srcset") or "")
        url = absolute_url(base_url, source or "")
        if url is None or url in images:
            continue
        images[url] = Image(
            url=url,
            alt=_WHITESPACE.sub(" ", attrs.get("alt") or "").strip(),
            width=_dimension(attrs.get("width")),
            height=_dimension(attrs.get("height")),
        )
    return tuple(images.values())
