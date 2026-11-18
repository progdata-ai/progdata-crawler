"""The response envelope used by every entry point, and the error codes."""

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErrorCode(StrEnum):
    """Stable error codes. Clients should branch on these, not on messages."""

    INVALID_REQUEST = "invalid_request"
    """The request failed validation."""
    ROBOTS_DISALLOWED = "robots_disallowed"
    """robots.txt does not allow fetching the URL."""
    RATE_LIMITED = "rate_limited"
    """The target site asked us to slow down, or our own per-domain limit was hit."""
    FETCH_TIMEOUT = "fetch_timeout"
    """The page did not respond within the time limit."""
    FETCH_FAILED = "fetch_failed"
    """The page could not be fetched: DNS failure, refused connection, broken TLS and so on."""
    ADDRESS_NOT_ALLOWED = "address_not_allowed"
    """The URL points at an internal network address, which this service may not fetch."""
    BLOCKED_BY_SITE = "blocked_by_site"
    """The site refused automated access. We do not retry with other techniques."""
    UNSUPPORTED_TYPE = "unsupported_type"
    """The content type cannot be handled."""
    QUOTA_EXCEEDED = "quota_exceeded"
    """The API key has used up its quota."""
    INTERNAL_ERROR = "internal_error"
    """Something went wrong on our side."""

    @property
    def http_status(self) -> int:
        """The HTTP status the REST API returns with this error."""
        return _HTTP_STATUS[self]


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.ROBOTS_DISALLOWED: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.FETCH_TIMEOUT: 504,
    ErrorCode.FETCH_FAILED: 502,
    ErrorCode.ADDRESS_NOT_ALLOWED: 403,
    ErrorCode.BLOCKED_BY_SITE: 502,
    ErrorCode.UNSUPPORTED_TYPE: 415,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ErrorInfo(BaseModel):
    """What went wrong."""

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str
    """Human-readable explanation. May change between versions."""
    details: dict[str, Any] = Field(default_factory=dict[str, Any])
    """Extra machine-readable context, such as the URL or a retry delay."""


class Meta(BaseModel):
    """Facts about how a request was handled."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(min_length=1)
    took_ms: int = Field(ge=0)
    cached: bool = False
    """The result came from the cache."""
    rendered: bool = False
    """The page was loaded in a browser."""


class Envelope[DataT](BaseModel):
    """Every response has the same shape: either `data` or `error` is set, never both."""

    model_config = ConfigDict(frozen=True)

    data: DataT | None = None
    meta: Meta
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _exactly_one_of_data_or_error(self) -> Self:
        if (self.data is None) == (self.error is None):
            raise ValueError("exactly one of data and error must be set")
        return self
