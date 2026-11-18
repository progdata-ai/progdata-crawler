import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.polite.ratelimit import DomainRateLimiter, parse_retry_after
from progdata_crawler.models import ErrorCode


class FakeTime:
    """A clock that only moves when the limiter sleeps. Records every sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = 1_800_000_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def wall_clock(self) -> float:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.wall += seconds
        await asyncio.sleep(0)


def limiter(time: FakeTime, **kwargs: float) -> DomainRateLimiter:
    return DomainRateLimiter(
        clock=time.clock, wall_clock=time.wall_clock, sleep=time.sleep, **kwargs
    )


async def test_first_request_does_not_wait() -> None:
    time = FakeTime()
    await limiter(time).acquire("https://example.com/")
    assert time.sleeps == []


async def test_requests_to_the_same_host_are_spaced_out() -> None:
    time = FakeTime()
    rate = limiter(time, min_interval_s=1.0)
    for path in ("a", "b", "c"):
        await rate.acquire(f"https://example.com/{path}")
    assert time.sleeps == [1.0, 1.0]


async def test_concurrent_requests_are_serialized_per_host() -> None:
    time = FakeTime()
    rate = limiter(time, min_interval_s=0.5)
    started: list[float] = []

    async def request(i: int) -> None:
        await rate.acquire(f"https://example.com/{i}")
        started.append(time.now)

    await asyncio.gather(*(request(i) for i in range(4)))
    assert started == [1000.0, 1000.5, 1001.0, 1001.5]


async def test_hosts_are_independent() -> None:
    time = FakeTime()
    rate = limiter(time)
    await rate.acquire("https://example.com/")
    await rate.acquire("https://other.example/")
    await rate.acquire("https://EXAMPLE.com:8443/")  # same host, different port
    assert time.sleeps == [1.0]


async def test_crawl_delay_wins_when_longer() -> None:
    time = FakeTime()
    rate = limiter(time, min_interval_s=1.0)
    await rate.acquire("https://example.com/", crawl_delay=5)
    await rate.acquire("https://example.com/", crawl_delay=5)
    await rate.acquire("https://example.com/", crawl_delay=0.1)
    assert time.sleeps == [5.0, 5.0]


async def test_retry_after_seconds_is_respected() -> None:
    time = FakeTime()
    rate = limiter(time)
    await rate.acquire("https://example.com/")
    assert rate.record_response("https://example.com/", 429, "30") == 30
    await rate.acquire("https://example.com/")
    assert time.sleeps == [30.0]


async def test_retry_after_http_date_is_respected() -> None:
    time = FakeTime()
    rate = limiter(time)
    later = datetime.fromtimestamp(time.wall + 45, tz=UTC)
    assert (
        rate.record_response("https://example.com/", 503, format_datetime(later, usegmt=True)) == 45
    )


async def test_exponential_backoff_without_retry_after() -> None:
    time = FakeTime()
    rate = limiter(time, backoff_base_s=2, backoff_max_s=10)
    delays = [rate.record_response("https://example.com/", 429) for _ in range(5)]
    assert delays == [2, 4, 8, 10, 10]


async def test_success_resets_backoff() -> None:
    time = FakeTime()
    rate = limiter(time, backoff_base_s=2)
    rate.record_response("https://example.com/", 429)
    rate.record_response("https://example.com/", 429)
    assert rate.record_response("https://example.com/", 200) == 0
    assert rate.record_response("https://example.com/", 429) == 2


async def test_other_errors_do_not_back_off() -> None:
    time = FakeTime()
    rate = limiter(time)
    assert rate.record_response("https://example.com/", 404) == 0
    assert rate.record_response("https://example.com/", 500) == 0


async def test_raises_rate_limited_instead_of_waiting_too_long() -> None:
    time = FakeTime()
    rate = limiter(time)
    rate.record_response("https://example.com/", 429, "3600")
    with pytest.raises(CrawlerError) as raised:
        await rate.acquire("https://example.com/page", max_wait_s=60)
    assert raised.value.code is ErrorCode.RATE_LIMITED
    assert raised.value.details == {"url": "https://example.com/page", "retry_after_s": 3600.0}
    assert time.sleeps == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("120", 120.0),
        (" 7 ", 7.0),
        ("soon", None),
        ("-5", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # in the past
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, now=1_800_000_000.0) == expected
