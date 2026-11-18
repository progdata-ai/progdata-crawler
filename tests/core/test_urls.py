import pytest

from progdata_crawler.core.urls import (
    UrlDeduplicator,
    canonical_key,
    normalize_url,
    same_site,
)

SAME_PAGE = [
    # (variant, normalized)
    ("https://example.com/docs/page", "https://example.com/docs/page"),
    ("HTTPS://Example.COM/docs/page", "https://example.com/docs/page"),
    ("https://example.com:443/docs/page", "https://example.com/docs/page"),
    ("http://example.com:80/docs/page", "http://example.com/docs/page"),
    ("https://example.com./docs/page", "https://example.com/docs/page"),
    ("https://user:secret@example.com/docs/page", "https://example.com/docs/page"),
    ("https://example.com/docs/page#section-2", "https://example.com/docs/page"),
    ("https://example.com/docs/./guide/../page", "https://example.com/docs/page"),
    ("https://example.com/../docs/page", "https://example.com/docs/page"),
    ("https://example.com/%7Euser/%61bc", "https://example.com/~user/abc"),
    ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
    ("https://example.com", "https://example.com/"),
    ("https://example.com?", "https://example.com/"),
    ("https://example.com/s?b=2&a=1", "https://example.com/s?a=1&b=2"),
    ("https://example.com/s?a=1&utm_source=x&UTM_Medium=y", "https://example.com/s?a=1"),
    ("https://example.com/s?gclid=abc&fbclid=def&id=7", "https://example.com/s?id=7"),
    ("https://example.com/s?q=", "https://example.com/s?q="),
    ("https://example.com/s?q=a+b", "https://example.com/s?q=a+b"),
    ("https://blåbær.no/side", "https://xn--blbr-roah.no/side"),
    ("http://[::1]:8080/x", "http://[::1]:8080/x"),
    ("  https://example.com/x  ", "https://example.com/x"),
]


@pytest.mark.parametrize(("variant", "normalized"), SAME_PAGE)
def test_normalize_url(variant: str, normalized: str) -> None:
    assert normalize_url(variant) == normalized


def test_normalizing_twice_changes_nothing() -> None:
    for variant, normalized in SAME_PAGE:
        assert normalize_url(normalize_url(variant)) == normalized


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/Docs",  # paths are case-sensitive
        "https://example.com/docs/",  # trailing slash may be a different page
        "https://www.example.com/docs",  # www is a different host
        "https://example.com:8443/docs",  # non-default port
        "http://example.com/docs",  # different scheme
        "https://example.com/docs?ref=x",  # unknown parameters are kept
    ],
)
def test_different_pages_stay_different(url: str) -> None:
    assert normalize_url(url) != normalize_url("https://example.com/docs")


@pytest.mark.parametrize("url", ["ftp://example.com/", "mailto:a@b.c", "/relative", "", "https://"])
def test_rejects_non_http_urls(url: str) -> None:
    with pytest.raises(ValueError, match="not an http"):
        normalize_url(url)


def test_same_site() -> None:
    assert same_site("https://www.example.com/a", "https://example.com/b")
    assert not same_site("https://example.com/", "https://example.org/")
    assert not same_site("", "")


def test_canonical_key() -> None:
    page = "https://example.com/article?utm_source=feed"
    assert (
        canonical_key(page, "https://www.example.com/article") == "https://www.example.com/article"
    )
    assert canonical_key(page, None) == "https://example.com/article"
    # A canonical on another site is ignored.
    assert canonical_key(page, "https://evil.example/article") == "https://example.com/article"
    assert canonical_key(page, "javascript:alert(1)") == "https://example.com/article"


def test_deduplicator() -> None:
    seen = UrlDeduplicator()
    assert seen.add("https://example.com/docs?utm_campaign=x")
    assert not seen.add("HTTPS://EXAMPLE.com/docs#top")
    assert seen.add("https://example.com/docs/other")
    assert "https://example.com/docs" in seen
    assert 42 not in seen
    assert len(seen) == 2
