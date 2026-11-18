"""The page cache: reuse a recent result for the same URL and options.

The cache key covers the URL and every option that changes the result. Options that
only say how long to wait or how old a result may be (`timeout_ms`, `cache_ttl`,
`retain`) are not part of the key.
"""

import hashlib
import json

from progdata_crawler.core.store.sqlite import CachedPage, PageStore
from progdata_crawler.models.options import PageOptions, Retention
from progdata_crawler.models.page import Page

KEY_FIELDS = ("outputs", "content", "tags", "wait_ms", "device", "locale", "geo", "render")


def cache_key(url: str, options: PageOptions) -> str:
    relevant = options.model_dump(mode="json", include=set(KEY_FIELDS))
    relevant["outputs"] = sorted(relevant["outputs"])
    payload = json.dumps({"url": url, **relevant}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class PageCache:
    def __init__(self, store: PageStore) -> None:
        self._store = store

    async def get(self, url: str, options: PageOptions) -> CachedPage | None:
        """A stored result no older than `options.cache_ttl`. `cache_ttl=0` never hits."""
        if options.cache_ttl == 0:
            return None
        return await self._store.get(cache_key(url, options), options.cache_ttl)

    async def put(self, url: str, options: PageOptions, page: Page, *, rendered: bool) -> None:
        """Store a result. Nothing is stored when `retain` is `none`."""
        if options.retain is Retention.NONE:
            return
        title = page.metadata.title if page.metadata is not None else None
        await self._store.put(
            cache_key(url, options), page, rendered=rendered, title=title, body=page.markdown
        )
