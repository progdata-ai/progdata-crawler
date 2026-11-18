"""Jobs: long-running work such as harvest and bulk, run in the background."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from progdata_crawler.models.envelope import ErrorInfo
from progdata_crawler.models.options import Retention
from progdata_crawler.models.page import Page


class JobKind(StrEnum):
    HARVEST = "harvest"
    BULK = "bulk"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    """The service stopped while the job was running."""

    @property
    def finished(self) -> bool:
        return self not in (JobStatus.QUEUED, JobStatus.RUNNING)


class JobProgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int | None = None
    """Pages expected, when known."""
    done: int = 0
    """Pages fetched successfully."""
    failed: int = 0
    """Pages that ended in an error."""


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: JobKind
    status: JobStatus
    created_at: float
    """Unix time."""
    started_at: float | None = None
    finished_at: float | None = None
    progress: JobProgress = JobProgress()
    error: ErrorInfo | None = None
    """Why the job as a whole failed. Errors for single pages are in the results."""
    retain: Retention = Retention.DEFAULT
    request: dict[str, Any] = Field(default_factory=dict[str, Any])
    """The request that started the job."""


class JobResult(BaseModel):
    """One page of a job: the page, or why it could not be fetched."""

    model_config = ConfigDict(frozen=True)

    seq: int
    """Position in the job, from 1. Use it as the cursor for the next request."""
    url: str
    page: Page | None = None
    error: ErrorInfo | None = None


MAX_RESULTS_PER_REQUEST = 1_000


class JobView(BaseModel):
    """A job's status and one page of its results."""

    model_config = ConfigDict(frozen=True)

    job: Job
    results: tuple[JobResult, ...] = ()
    next_cursor: int
    """Pass as `cursor` to get the results after these."""
    complete: bool
    """True when the job has finished and every result has been returned."""


class JobEventType(StrEnum):
    JOB = "job"
    """The job's status or progress changed. Also the first event of every stream."""
    RESULT = "result"
    """A page finished."""
    KEEP_ALIVE = "keep_alive"
    """Nothing happened for a while; keeps proxies from closing the connection."""


class JobEvent(BaseModel):
    """One event in a job's event stream."""

    model_config = ConfigDict(frozen=True)

    id: int
    """The `seq` of the last result sent so far. Resume from here after a disconnect."""
    type: JobEventType
    job: Job | None = None
    result: JobResult | None = None
