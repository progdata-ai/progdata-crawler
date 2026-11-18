"""Watches and their versions in SQLite."""

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from progdata_crawler.models.watch import CheckStatus, Watch

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id              TEXT PRIMARY KEY,
    url             TEXT NOT NULL,
    interval_s      INTEGER NOT NULL,
    notify_url      TEXT,
    options         TEXT NOT NULL,
    created_at      REAL NOT NULL,
    next_check_at   REAL NOT NULL,
    last_checked_at REAL,
    last_changed_at REAL,
    last_status     TEXT,
    checks          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS watches_next_check_at ON watches (next_check_at);
CREATE TABLE IF NOT EXISTS watch_versions (
    watch_id   TEXT NOT NULL REFERENCES watches (id) ON DELETE CASCADE,
    checked_at REAL NOT NULL,
    markdown   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS watch_versions_watch ON watch_versions (watch_id, checked_at);
"""

_COLUMNS = (
    "id, url, interval_s, notify_url, options, created_at, next_check_at, "
    "last_checked_at, last_changed_at, last_status, checks"
)


class WatchStore:
    """Synchronous SQLite access, guarded by a lock. Callers run it in a worker thread."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(row: tuple[Any, ...]) -> Watch:
        (id_, url, interval, notify, options, created, next_at, checked, changed, status, n) = row
        return Watch(
            id=id_,
            url=url,
            interval_s=interval,
            notify_url=notify,
            options=json.loads(options),
            created_at=created,
            next_check_at=next_at,
            last_checked_at=checked,
            last_changed_at=changed,
            last_status=CheckStatus(status) if status else None,
            checks=n,
        )

    def create(
        self, url: str, interval_s: int, options: dict[str, Any], notify_url: str | None
    ) -> Watch:
        now = self._clock()
        watch_id = secrets.token_hex(8)
        with self._lock:
            self._db.execute(
                "INSERT INTO watches (id, url, interval_s, notify_url, options, created_at,"
                " next_check_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (watch_id, url, interval_s, notify_url, json.dumps(options), now, now),
            )
        watch = self.get(watch_id)
        assert watch is not None
        return watch

    def get(self, watch_id: str) -> Watch | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM watches WHERE id = ?",
                (watch_id,),
            ).fetchone()
        return self._row(row) if row else None

    def all(self) -> list[Watch]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM watches ORDER BY created_at"
            ).fetchall()
        return [self._row(row) for row in rows]

    def due(self) -> list[Watch]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM watches WHERE next_check_at <= ? ORDER BY next_check_at",
                (self._clock(),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def claim(self, watch: Watch, lease_s: float) -> bool:
        """Take a due watch for checking, so other processes sharing this database skip
        it: `next_check_at` moves `lease_s` ahead, but only if no one changed it since
        `watch` was read. Atomic across processes. If the claimer dies, the lease runs
        out and the watch is due again."""
        with self._lock:
            return (
                self._db.execute(
                    "UPDATE watches SET next_check_at = ? WHERE id = ? AND next_check_at = ?",
                    (self._clock() + lease_s, watch.id, watch.next_check_at),
                ).rowcount
                == 1
            )

    def next_due_at(self) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(next_check_at) FROM watches").fetchone()
        return row[0] if row else None

    def delete(self, watch_id: str) -> bool:
        """Delete the watch and all its versions."""
        with self._lock:
            return self._db.execute("DELETE FROM watches WHERE id = ?", (watch_id,)).rowcount > 0

    def latest_version(self, watch_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT markdown FROM watch_versions WHERE watch_id = ? "
                "ORDER BY checked_at DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
        return row[0] if row else None

    def record(self, watch: Watch, status: CheckStatus, *, markdown: str | None, at: float) -> None:
        """Store a check's outcome, and a new version when the page is new or changed."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                if markdown is not None and status in (CheckStatus.FIRST, CheckStatus.CHANGED):
                    self._db.execute(
                        "INSERT INTO watch_versions (watch_id, checked_at, markdown) "
                        "VALUES (?, ?, ?)",
                        (watch.id, at, markdown),
                    )
                self._db.execute(
                    "UPDATE watches SET next_check_at = ?, last_checked_at = ?, "
                    "last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END, "
                    "last_status = ?, checks = checks + 1 WHERE id = ?",
                    (
                        at + watch.interval_s,
                        at,
                        status is CheckStatus.CHANGED,
                        at,
                        status.value,
                        watch.id,
                    ),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def delete_versions_older_than(self, max_age_s: float) -> int:
        """Delete versions older than `max_age_s`, but always keep the newest version of
        each watch: it is what the next check compares with. Returns versions deleted."""
        oldest = self._clock() - max_age_s
        with self._lock:
            return self._db.execute(
                "DELETE FROM watch_versions WHERE checked_at < ? AND rowid NOT IN ("
                " SELECT v.rowid FROM watch_versions v WHERE v.checked_at = ("
                "  SELECT MAX(w.checked_at) FROM watch_versions w WHERE w.watch_id = v.watch_id"
                " ))",
                (oldest,),
            ).rowcount

    def versions(self, watch_id: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM watch_versions WHERE watch_id = ?", (watch_id,)
            ).fetchone()[0]
