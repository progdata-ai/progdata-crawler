"""Local storage in SQLite: cached page results and a full-text index of their content.

SQLite calls are blocking, so the async methods run them in a worker thread. One
connection is shared and guarded by a lock.
"""

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from progdata_crawler.models.page import Page

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    cache_key  TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    rendered   INTEGER NOT NULL,
    page_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_fetched_at ON pages (fetched_at);
CREATE VIRTUAL TABLE IF NOT EXISTS page_text USING fts5(
    url UNINDEXED,
    title,
    body,
    fetched_at UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class CachedPage:
    page: Page
    fetched_at: float
    """Unix time of the fetch."""
    rendered: bool


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    """A short excerpt with the matches in [brackets]."""
    fetched_at: float


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word must match, no operators."""
    words = [word.replace('"', '""') for word in text.split()]
    return " ".join(f'"{word}"' for word in words)


class PageStore:
    """SQLite storage. Pass a file path, or ":memory:" for a throwaway store."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    async def _run[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return work(self._db)

        return await asyncio.to_thread(locked)

    async def get(self, cache_key: str, max_age_s: float) -> CachedPage | None:
        """The stored result for `cache_key` if it is at most `max_age_s` old."""
        oldest = self._clock() - max_age_s

        def work(db: sqlite3.Connection) -> tuple[str, float, int] | None:
            return db.execute(
                "SELECT page_json, fetched_at, rendered FROM pages "
                "WHERE cache_key = ? AND fetched_at >= ?",
                (cache_key, oldest),
            ).fetchone()

        row = await self._run(work)
        if row is None:
            return None
        page_json, fetched_at, rendered = row
        return CachedPage(Page.model_validate_json(page_json), fetched_at, bool(rendered))

    async def put(
        self,
        cache_key: str,
        page: Page,
        *,
        rendered: bool,
        title: str | None = None,
        body: str | None = None,
    ) -> None:
        """Store a result. With `body`, the page's text is (re)indexed for search."""
        now = self._clock()
        page_json = page.model_dump_json()

        def work(db: sqlite3.Connection) -> None:
            db.execute("BEGIN")
            try:
                db.execute(
                    "INSERT OR REPLACE INTO pages (cache_key, url, fetched_at, rendered, page_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (cache_key, page.url, now, int(rendered), page_json),
                )
                if body is not None:
                    db.execute("DELETE FROM page_text WHERE url = ?", (page.url,))
                    db.execute(
                        "INSERT INTO page_text (url, title, body, fetched_at) VALUES (?, ?, ?, ?)",
                        (page.url, title or "", body, now),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

        await self._run(work)

    async def search(self, text: str, *, limit: int = 10) -> list[SearchHit]:
        """Full-text search in stored pages, best matches first."""
        query = fts_query(text)
        if not query:
            return []

        def work(db: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
            return db.execute(
                "SELECT url, title, snippet(page_text, 2, '[', ']', '…', 16), fetched_at "
                "FROM page_text WHERE page_text MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()

        rows = await self._run(work)
        return [
            SearchHit(url, title, snippet, fetched_at) for url, title, snippet, fetched_at in rows
        ]

    async def delete_older_than(self, max_age_s: float) -> int:
        """Delete results and indexed text older than `max_age_s`. Returns rows deleted."""
        oldest = self._clock() - max_age_s

        def work(db: sqlite3.Connection) -> int:
            db.execute("BEGIN")
            try:
                deleted = db.execute("DELETE FROM pages WHERE fetched_at < ?", (oldest,)).rowcount
                db.execute("DELETE FROM page_text WHERE fetched_at < ?", (oldest,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return deleted

        return await self._run(work)
