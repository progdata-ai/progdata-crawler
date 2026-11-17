import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models import ErrorCode


def test_crawler_error_carries_code_message_and_details() -> None:
    with pytest.raises(CrawlerError) as raised:
        raise CrawlerError(
            ErrorCode.ROBOTS_DISALLOWED,
            "robots.txt disallows this URL",
            url="https://example.com/private",
        )

    error = raised.value
    assert str(error) == "robots.txt disallows this URL"
    assert error.code is ErrorCode.ROBOTS_DISALLOWED
    assert error.details == {"url": "https://example.com/private"}


def test_converts_to_error_info() -> None:
    info = CrawlerError(ErrorCode.RATE_LIMITED, "slow down", retry_after_s=2).to_error_info()
    assert info.model_dump(mode="json") == {
        "code": "rate_limited",
        "message": "slow down",
        "details": {"retry_after_s": 2},
    }
