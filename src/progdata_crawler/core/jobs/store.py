"""The job table: jobs and their per-page results in SQLite."""

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from progdata_crawler.models.envelope import ErrorInfo
from progdata_crawler.models.jobs import Job, JobKind, JobProgress, JobResult, JobStatus
from progdata_crawler.models.options import Retention
from progdata_crawler.models.page import Page

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    total       INTEGER,
    done        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error_json  TEXT,
    retain      TEXT NOT NULL,
    request     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs (created_at);
CREATE TABLE IF NOT EXISTS job_results (
    job_id     TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    url        TEXT NOT NULL,
    page_json  TEXT,
    error_json TEXT,
    PRIMARY KEY (job_id, seq)
);
"""


class JobStore:
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

    def _row_to_job(self, row: tuple[Any, ...]) -> Job:
        (id_, kind, status, created, started, finished, total, done, failed, error, retain, req) = (
            row
        )
        return Job(
            id=id_,
            kind=JobKind(kind),
            status=JobStatus(status),
            created_at=created,
            started_at=started,
            finished_at=finished,
            progress=JobProgress(total=total, done=done, failed=failed),
            error=ErrorInfo.model_validate_json(error) if error else None,
            retain=Retention(retain),
            request=json.loads(req),
        )

    _JOB_COLUMNS = (
        "id, kind, status, created_at, started_at, finished_at, total, done, failed, "
        "error_json, retain, request"
    )

    def create(self, kind: JobKind, request: dict[str, Any], retain: Retention) -> Job:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, kind, status, created_at, retain, request)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    kind.value,
                    JobStatus.QUEUED.value,
                    self._clock(),
                    retain.value,
                    json.dumps(request),
                ),
            )
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {self._JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def ids_with_status(self, status: JobStatus) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at", (status.value,)
            ).fetchall()
        return [row[0] for row in rows]

    def mark_started(self, job_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                (JobStatus.RUNNING.value, self._clock(), job_id),
            )

    def mark_finished(self, job_id: str, status: JobStatus, error: ErrorInfo | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error_json = ? WHERE id = ?",
                (status.value, self._clock(), error.model_dump_json() if error else None, job_id),
            )

    def interrupt_unfinished(self) -> list[str]:
        """Mark jobs left running by a previous process as interrupted. Returns their ids."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status = ?", (JobStatus.RUNNING.value,)
            ).fetchall()
            self._db.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE status = ?",
                (JobStatus.INTERRUPTED.value, self._clock(), JobStatus.RUNNING.value),
            )
        return [row[0] for row in rows]

    def set_total(self, job_id: str, total: int | None) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET total = ? WHERE id = ?", (total, job_id))

    def add_result(
        self, job_id: str, url: str, page: Page | None = None, error: ErrorInfo | None = None
    ) -> int:
        """Store one page of a job and count it. Returns its sequence number."""
        counter = "done" if error is None else "failed"
        with self._lock:
            self._db.execute("BEGIN")
            try:
                (seq,) = self._db.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_results WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                self._db.execute(
                    "INSERT INTO job_results (job_id, seq, url, page_json, error_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        job_id,
                        seq,
                        url,
                        page.model_dump_json() if page else None,
                        error.model_dump_json() if error else None,
                    ),
                )
                self._db.execute(
                    f"UPDATE jobs SET {counter} = {counter} + 1 WHERE id = ?", (job_id,)
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return int(seq)

    def results(self, job_id: str, *, after: int = 0, limit: int = 100) -> list[JobResult]:
        """Results with a sequence number above `after`, in order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, url, page_json, error_json FROM job_results"
                " WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (job_id, after, limit),
            ).fetchall()
        return [
            JobResult(
                seq=seq,
                url=url,
                page=Page.model_validate_json(page) if page else None,
                error=ErrorInfo.model_validate_json(error) if error else None,
            )
            for seq, url, page, error in rows
        ]

    def delete_results(self, job_id: str, *, up_to: int) -> int:
        """Delete results up to and including `up_to` (for `retain: none`)."""
        with self._lock:
            return self._db.execute(
                "DELETE FROM job_results WHERE job_id = ? AND seq <= ?", (job_id, up_to)
            ).rowcount

    def delete_older_than(self, max_age_s: float) -> int:
        """Delete finished jobs created more than `max_age_s` ago, with their results."""
        oldest = self._clock() - max_age_s
        finished = tuple(status.value for status in JobStatus if status.finished)
        placeholders = ",".join("?" * len(finished))
        with self._lock:
            return self._db.execute(
                f"DELETE FROM jobs WHERE created_at < ? AND status IN ({placeholders})",
                (oldest, *finished),
            ).rowcount
