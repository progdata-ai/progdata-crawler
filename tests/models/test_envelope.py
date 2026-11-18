import pytest
from pydantic import BaseModel, ValidationError

from progdata_crawler.models import Envelope, ErrorCode, ErrorInfo, Meta


class _Page(BaseModel):
    markdown: str


META = Meta(request_id="req-1", took_ms=12)


def test_success_envelope_round_trips_through_json() -> None:
    envelope = Envelope[_Page](data=_Page(markdown="# Hello"), meta=META)
    dumped = envelope.model_dump(mode="json")
    assert dumped == {
        "data": {"markdown": "# Hello"},
        "meta": {"request_id": "req-1", "took_ms": 12, "cached": False, "rendered": False},
        "error": None,
    }
    assert Envelope[_Page].model_validate(dumped) == envelope


def test_error_envelope() -> None:
    error = ErrorInfo(
        code=ErrorCode.ROBOTS_DISALLOWED,
        message="robots.txt disallows this URL",
        details={"url": "https://example.com/private"},
    )
    envelope = Envelope[_Page](meta=META, error=error)
    assert envelope.data is None
    assert envelope.model_dump(mode="json")["error"]["code"] == "robots_disallowed"


def test_envelope_needs_exactly_one_of_data_or_error() -> None:
    error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="boom")
    with pytest.raises(ValidationError, match="exactly one of data and error"):
        Envelope[_Page](meta=META)
    with pytest.raises(ValidationError, match="exactly one of data and error"):
        Envelope[_Page](data=_Page(markdown=""), meta=META, error=error)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.INVALID_REQUEST, 422),
        (ErrorCode.ROBOTS_DISALLOWED, 403),
        (ErrorCode.RATE_LIMITED, 429),
        (ErrorCode.FETCH_TIMEOUT, 504),
        (ErrorCode.FETCH_FAILED, 502),
        (ErrorCode.ADDRESS_NOT_ALLOWED, 403),
        (ErrorCode.BLOCKED_BY_SITE, 502),
        (ErrorCode.UNSUPPORTED_TYPE, 415),
        (ErrorCode.QUOTA_EXCEEDED, 429),
        (ErrorCode.INTERNAL_ERROR, 500),
    ],
)
def test_every_error_code_has_an_http_status(code: ErrorCode, status: int) -> None:
    assert code.http_status == status


def test_error_code_values_are_stable() -> None:
    assert [code.value for code in ErrorCode] == [
        "invalid_request",
        "robots_disallowed",
        "rate_limited",
        "fetch_timeout",
        "fetch_failed",
        "address_not_allowed",
        "blocked_by_site",
        "unsupported_type",
        "quota_exceeded",
        "internal_error",
    ]


def test_meta_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError):
        Meta(request_id="req-1", took_ms=-1)
