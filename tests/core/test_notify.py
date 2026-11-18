import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.notify import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookNotifier,
    sign,
    verify,
)
from progdata_crawler.core.jobs.queue import JobContext
from progdata_crawler.models import ErrorCode, Job, JobKind, JobStatus, Page
from progdata_crawler.settings import Settings, default_user_agent
from tests.support.site_server import Received, SiteServer

SECRET = "test-secret"


async def succeeds(job: Job, ctx: JobContext) -> None:
    await ctx.report("https://example.test/", page=Page(url="https://example.test/", markdown="x"))


async def fails(job: Job, ctx: JobContext) -> None:
    raise CrawlerError(ErrorCode.BLOCKED_BY_SITE, "blocked")


async def runs_forever(job: Job, ctx: JobContext) -> None:
    await asyncio.Event().wait()


def notifier(
    *, allow_private: bool = True, delays: tuple[float, ...] = (0.05, 0.05)
) -> WebhookNotifier:
    return WebhookNotifier(
        SECRET,
        default_user_agent(),
        AddressGuard(allow_private=allow_private),
        retry_delays_s=delays,
        deadline_s=5,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, webhook_secret=SECRET)  # pyright: ignore[reportArgumentType]


async def run_jobs(settings: Settings, *requests: tuple[JobKind, dict[str, Any]]) -> list[Job]:
    handlers = {JobKind.BULK: succeeds, JobKind.HARVEST: fails}
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(settings, handlers, page_store=crawler.store, notifier=notifier()) as jobs,
    ):
        submitted = [await jobs.submit(kind, request) for kind, request in requests]
        finished = [await jobs.wait(job.id) for job in submitted]
    return [job for job in finished if job is not None]


def events(received: list[Received], path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for request in received:
        if request.path != path:
            continue
        timestamp = int(request.headers[TIMESTAMP_HEADER.lower()])
        signature = request.headers[SIGNATURE_HEADER.lower()]
        assert verify(SECRET, timestamp, request.body, signature)
        body = json.loads(request.body)
        assert request.headers[EVENT_HEADER.lower()] == body["event"]
        out.append(body)
    return out


async def test_started_finished_and_failed_events(
    settings: Settings, test_site: SiteServer
) -> None:
    ok, failed = await run_jobs(
        settings,
        (JobKind.BULK, {"notify_url": test_site.url("/hooks/ok")}),
        (JobKind.HARVEST, {"notify_url": test_site.url("/hooks/failed")}),
    )
    ok_events = events(test_site.received, "/hooks/ok")
    assert [e["event"] for e in ok_events] == ["job.started", "job.succeeded"]
    assert ok_events[-1]["job"]["id"] == ok.id
    assert ok_events[-1]["job"]["progress"]["done"] == 1
    # Status only: no request and no page content.
    assert "request" not in ok_events[-1]["job"]
    assert b"markdown" not in test_site.received[0].body

    failed_events = events(test_site.received, "/hooks/failed")
    assert [e["event"] for e in failed_events] == ["job.started", "job.failed"]
    assert failed_events[-1]["job"]["error"]["code"] == "blocked_by_site"
    assert failed.status is JobStatus.FAILED


async def test_cancelled_event(settings: Settings, test_site: SiteServer) -> None:
    handlers = {JobKind.HARVEST: runs_forever}
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(settings, handlers, page_store=crawler.store, notifier=notifier()) as jobs,
    ):
        job = await jobs.submit(JobKind.HARVEST, {"notify_url": test_site.url("/hooks/c")})
        while (
            current := await jobs.get(job.id)
        ) is not None and current.status is JobStatus.QUEUED:
            await asyncio.sleep(0.01)
        await jobs.cancel(job.id)
    assert [e["event"] for e in events(test_site.received, "/hooks/c")] == [
        "job.started",
        "job.cancelled",
    ]


def a_job() -> Job:
    return Job(id="j1", kind=JobKind.BULK, status=JobStatus.SUCCEEDED, created_at=0)


async def test_retries_server_errors(test_site: SiteServer) -> None:
    url = test_site.add("/hooks/flaky", "busy", status=503)
    sender = notifier()

    async def recover() -> None:
        while not test_site.received:
            await asyncio.sleep(0.01)
        test_site.add("/hooks/flaky", "ok", status=200)

    delivered, _ = await asyncio.gather(sender.deliver(url, a_job()), recover())
    await sender.aclose()
    assert delivered
    assert len(test_site.received) == 2
    # Each attempt is signed with its own timestamp.
    for request in test_site.received:
        timestamp = int(request.headers[TIMESTAMP_HEADER.lower()])
        assert request.headers[SIGNATURE_HEADER.lower()] == sign(SECRET, timestamp, request.body)


async def test_gives_up_after_the_last_attempt(test_site: SiteServer) -> None:
    url = test_site.add("/hooks/down", "busy", status=503)
    sender = notifier(delays=(0.01, 0.01))
    assert not await sender.deliver(url, a_job())
    await sender.aclose()
    assert len(test_site.received) == 3


async def test_client_errors_are_not_retried(test_site: SiteServer) -> None:
    url = test_site.add("/hooks/gone", "no", status=410)
    sender = notifier()
    assert not await sender.deliver(url, a_job())
    await sender.aclose()
    assert len(test_site.received) == 1


async def test_internal_addresses_are_refused(test_site: SiteServer) -> None:
    sender = notifier(allow_private=False)
    assert not await sender.deliver(test_site.url("/hooks/x"), a_job())
    await sender.aclose()
    assert test_site.received == []


async def test_submit_refuses_internal_notify_url(
    tmp_path: Path, test_site: SiteServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PDC_ALLOW_PRIVATE_NETWORKS", "false")
    settings = Settings(data_dir=tmp_path, webhook_secret=SECRET)  # pyright: ignore[reportArgumentType]
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(settings, {}, page_store=crawler.store) as jobs,
    ):
        with pytest.raises(CrawlerError) as raised:
            await jobs.submit(JobKind.BULK, {"notify_url": test_site.url("/hooks/x")})
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED


async def test_notify_url_needs_a_secret(tmp_path: Path, test_site: SiteServer) -> None:
    settings = Settings(data_dir=tmp_path)
    async with (
        Crawler(settings, store_path=":memory:") as crawler,
        JobManager(settings, {}, page_store=crawler.store) as jobs,
    ):
        with pytest.raises(CrawlerError) as raised:
            await jobs.submit(JobKind.BULK, {"notify_url": test_site.url("/hooks/x")})
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_verify_rejects_old_or_tampered_messages() -> None:
    import time

    now = int(time.time())
    body = b'{"event":"job.succeeded"}'
    signature = sign(SECRET, now, body)
    assert verify(SECRET, now, body, signature)
    assert not verify(SECRET, now, body + b" ", signature)
    assert not verify("other", now, body, signature)
    old = now - 3600
    assert not verify(SECRET, old, body, sign(SECRET, old, body))
