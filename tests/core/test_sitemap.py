import gzip

import pytest

from progdata_crawler.core.discover.sitemap import decompress, parse_sitemap
from progdata_crawler.core.errors import CrawlerError

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc> https://example.test/a </loc><lastmod>2026-11-01</lastmod></url>
  <url><loc>https://example.test/b</loc></url>
  <url><lastmod>2026-11-02</lastmod></url>
</urlset>
"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.test/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>https://example.test/sitemap-2.xml.gz</loc></sitemap>
</sitemapindex>
"""


def test_urlset() -> None:
    content = parse_sitemap(URLSET)
    assert content.pages == [
        ("https://example.test/a", "2026-11-01"),
        ("https://example.test/b", None),
    ]
    assert content.sitemaps == []


def test_sitemap_index() -> None:
    content = parse_sitemap(INDEX)
    assert content.sitemaps == [
        "https://example.test/sitemap-1.xml",
        "https://example.test/sitemap-2.xml.gz",
    ]
    assert content.pages == []


def test_gzipped_sitemap() -> None:
    assert parse_sitemap(gzip.compress(URLSET)).pages[1] == ("https://example.test/b", None)


def test_plain_text_sitemap() -> None:
    text = b"https://example.test/one\n\nnot a url\nhttps://example.test/two\n"
    assert [loc for loc, _ in parse_sitemap(text).pages] == [
        "https://example.test/one",
        "https://example.test/two",
    ]


def test_broken_xml_counts_as_empty() -> None:
    content = parse_sitemap(b"<urlset><url><loc>https://example.test/a</loc>")
    assert (content.pages, content.sitemaps) == ([], [])


def test_external_entities_are_not_loaded(tmp_path: object) -> None:
    hostile = b"""<?xml version="1.0"?>
<!DOCTYPE urlset [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/&secret;</loc></url>
</urlset>
"""
    content = parse_sitemap(hostile)
    assert all("root:" not in loc for loc, _ in content.pages)


def test_entity_expansion_bomb_is_refused() -> None:
    entities = '<!ENTITY a0 "lol">' + "".join(
        f'<!ENTITY a{i} "{f"&a{i - 1};" * 10}">' for i in range(1, 10)
    )
    bomb = (
        f'<?xml version="1.0"?><!DOCTYPE urlset [{entities}]>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://example.test/&a9;</loc></url></urlset>"
    ).encode()
    content = parse_sitemap(bomb)
    assert content.pages == []


def test_gzip_bomb_is_refused() -> None:
    bomb = gzip.compress(b"\0" * (2 * 1024 * 1024))
    with pytest.raises(CrawlerError) as raised:
        decompress(bomb, limit=1024 * 1024)
    assert raised.value.details["max_bytes"] == 1024 * 1024


def test_corrupt_gzip_counts_as_empty() -> None:
    assert decompress(b"\x1f\x8b not really gzip") == b""
