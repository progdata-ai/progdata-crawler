"""Reading sitemaps safely.

Sitemaps come from sites we do not control, so parsing is defensive:

- The standard-library parser (expat 2.4.1 or newer) does not load external entities
  and refuses entity expansion bombs ("billion laughs"). A test guards this.
- `.gz` sitemaps are decompressed with a size limit, so a small file cannot expand
  into gigabytes (a zip bomb).
- A sitemap that cannot be parsed counts as empty instead of failing the whole discover.
"""

import gzip
import io
from dataclasses import dataclass, field
from xml.etree import ElementTree

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import ErrorCode

MAX_SITEMAP_BYTES = 10 * 1024 * 1024
"""Largest sitemap we download (compressed or not)."""

MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
"""The sitemap protocol's own limit for an uncompressed sitemap."""

GZIP_MAGIC = b"\x1f\x8b"


@dataclass
class SitemapContent:
    pages: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])
    """(location, lastmod) of each page."""
    sitemaps: list[str] = field(default_factory=list[str])
    """Child sitemaps, from a sitemap index."""


def decompress(content: bytes, limit: int = MAX_UNCOMPRESSED_BYTES) -> bytes:
    """Return `content`, gunzipped if it is gzip. Refuses output larger than `limit`."""
    if not content.startswith(GZIP_MAGIC):
        return content
    output = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
            while chunk := stream.read(64 * 1024):
                output += chunk
                if len(output) > limit:
                    raise CrawlerError(
                        ErrorCode.UNSUPPORTED_TYPE,
                        f"the sitemap is larger than {limit} bytes when decompressed",
                        max_bytes=limit,
                    )
    except (OSError, EOFError):
        return b""
    return bytes(output)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


def parse_sitemap(content: bytes) -> SitemapContent:
    """Parse an XML sitemap, a sitemap index, or a plain-text list of URLs."""
    data = decompress(content)
    result = SitemapContent()
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        text = data.decode("utf-8", errors="replace")
        if text.lstrip().startswith("<"):
            return result  # broken or hostile XML: treat as empty
        result.pages = [
            (line.strip(), None)
            for line in text.splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
        return result

    kind = _local(root.tag)
    for entry in root:
        loc = _child_text(entry, "loc")
        if not loc:
            continue
        if kind == "sitemapindex" and _local(entry.tag) == "sitemap":
            result.sitemaps.append(loc)
        elif kind == "urlset" and _local(entry.tag) == "url":
            result.pages.append((loc, _child_text(entry, "lastmod")))
    return result
