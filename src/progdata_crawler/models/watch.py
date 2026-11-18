"""Watch: re-fetch pages on a schedule and tell what changed."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from progdata_crawler.models.envelope import ErrorInfo
from progdata_crawler.models.options import PageOptions, Retention

MIN_INTERVAL_S = 300
MAX_INTERVAL_S = 30 * 24 * 3600


class WatchCreate(PageOptions):
    """Watch `url`: fetch it every `interval_s` seconds and compare with the last version.
    The page options apply to every fetch; the comparison uses the page's markdown."""

    url: HttpUrl
    interval_s: int = Field(default=3600, ge=MIN_INTERVAL_S, le=MAX_INTERVAL_S)
    notify_url: HttpUrl | None = None
    """Receives a signed POST with the diff when the page changes."""

    @model_validator(mode="after")
    def _keeps_versions(self) -> "WatchCreate":
        if self.retain is Retention.NONE:
            raise ValueError("a watch keeps versions to compare with, so retain cannot be none")
        if self.steps:
            raise ValueError("watches do not run browser steps")
        return self


class CheckStatus(StrEnum):
    FIRST = "first"
    """The first version: nothing to compare with yet."""
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    FAILED = "failed"
    """The page could not be fetched this time; see `error`."""


class Watch(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    url: str
    interval_s: int
    notify_url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict[str, Any])
    """The page options of every fetch."""
    created_at: float
    next_check_at: float
    last_checked_at: float | None = None
    last_changed_at: float | None = None
    last_status: CheckStatus | None = None
    checks: int = 0


class WatchCheck(BaseModel):
    """The outcome of one check of a watch."""

    model_config = ConfigDict(frozen=True)

    watch_id: str
    url: str
    checked_at: float
    status: CheckStatus
    diff: str | None = None
    """Unified diff of the markdown against the previous version, when it changed."""
    added_lines: int = 0
    removed_lines: int = 0
    error: ErrorInfo | None = None


class WatchList(BaseModel):
    model_config = ConfigDict(frozen=True)

    watches: tuple[Watch, ...]


class WatchResult(BaseModel):
    """What `pdc_watch` returns: the watch it acted on, the list, or a check."""

    model_config = ConfigDict(frozen=True)

    watch: Watch | None = None
    watches: tuple[Watch, ...] | None = None
    check: WatchCheck | None = None
