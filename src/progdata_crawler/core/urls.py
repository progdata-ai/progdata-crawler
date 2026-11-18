"""URL normalization and deduplication for discover and harvest.

Two URLs that point to the same page should give the same key, so a crawl never
fetches a page twice. `normalize_url` applies the safe rewrites from RFC 3986 plus a
few common ones (tracking parameters, fragments, sorted query). It never changes what
a server would serve, except for removing parameters that only track visitors.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}

TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
    }
)
TRACKING_PREFIXES = ("utm_",)

_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _normalize_percent(text: str) -> str:
    """Decode escapes of unreserved characters and uppercase the rest (RFC 3986 6.2.2)."""

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()

    return _PERCENT.sub(fix, text)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4: resolve `.` and `..` segments."""
    output: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(output) > 1:
                output.pop()
        elif segment != ".":
            output.append(segment)
    result = "/".join(output)
    if path.endswith(("/.", "/..")):
        result += "/"
    return result if result.startswith("/") else "/" + result


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def _host(hostname: str) -> str:
    host = hostname.lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def normalize_url(url: str) -> str:
    """The canonical form of an http(s) URL, used as its identity in a crawl.

    Lowercases scheme and host (IDN hosts become punycode), drops default ports, user
    info and the fragment, resolves `.`/`..`, normalizes percent-encoding, removes
    tracking parameters and sorts the rest of the query. Raises `ValueError` for URLs
    that are not http(s).
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {url!r}")

    netloc = _host(parts.hostname)
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    port = parts.port
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc += f":{port}"

    path = _remove_dot_segments(_normalize_percent(parts.path or "/"))

    pairs = [
        (_normalize_percent(name), _normalize_percent(value))
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    ]
    query = urlencode(sorted(pairs), safe="%/:@!$'()*,;") if pairs else ""

    return urlunsplit((scheme, netloc, path, query, ""))


def same_site(first: str, second: str) -> bool:
    """True when both URLs are on the same host, ignoring a leading `www.`."""
    host_a = (urlsplit(first).hostname or "").lower().removeprefix("www.")
    host_b = (urlsplit(second).hostname or "").lower().removeprefix("www.")
    return bool(host_a) and host_a == host_b


def canonical_key(page_url: str, canonical_url: str | None) -> str:
    """The key for a fetched page: its `<link rel=canonical>` when that points to the same
    site, otherwise the page's own URL. A canonical on another site is ignored, so a page
    cannot claim to be someone else's."""
    if canonical_url and same_site(page_url, canonical_url):
        try:
            return normalize_url(canonical_url)
        except ValueError:
            pass
    return normalize_url(page_url)


class UrlDeduplicator:
    """Remembers normalized URLs. `add` returns True the first time a URL is seen."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add(self, url: str) -> bool:
        key = normalize_url(url)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def __contains__(self, url: object) -> bool:
        return isinstance(url, str) and normalize_url(url) in self._seen

    def __len__(self) -> int:
        return len(self._seen)
