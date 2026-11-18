import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from progdata_crawler.api.rest import create_app
from progdata_crawler.core.jobs.manager import JobManager
from progdata_crawler.core.jobs.queue import JobContext
from progdata_crawler.models import Job, JobKind, Page, Retention
from progdata_crawler.settings import Settings

PAGES = 25


async def many_pages(job: Job, ctx: JobContext) -> None:
    await ctx.set_total(PAGES)
    for i in range(1, PAGES + 1):
        await ctx.report(f"https://example.test/{i}", page=Page(url=f"https://example.test/{i}"))


async def endless(job: Job, ctx: JobContext) -> None:
    """Reports a page now and then until it is cancelled."""
    i = 0
    while True:
        i += 1
        await ctx.report(f"https://example.test/{i}", page=Page(url=f"https://example.test/{i}"))
        await asyncio.sleep(0.05)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path)
    handlers = {JobKind.BULK: many_pages, JobKind.HARVEST: endless}
    app = create_app(settings, store_path=":memory:", job_handlers=handlers)
    with TestClient(app) as test_client:
        yield test_client


def manager(client: TestClient) -> JobManager:
    return cast(JobManager, cast(FastAPI, client.app).state.jobs)


def submit(client: TestClient, kind: JobKind, **options: Any) -> str:
    jobs = manager(client)
    assert client.portal is not None
    job = client.portal.call(lambda: jobs.submit(kind, {}, **options))
    return job.id


def wait(client: TestClient, job_id: str) -> None:
    assert client.portal is not None
    client.portal.call(manager(client).wait, job_id)


def test_results_are_read_page_by_page(client: TestClient) -> None:
    job_id = submit(client, JobKind.BULK)
    wait(client, job_id)

    urls: list[str] = []
    cursor, calls = 0, 0
    while True:
        response = client.get(f"/v1/jobs/{job_id}", params={"cursor": cursor, "limit": 10})
        assert response.status_code == 200
        view = response.json()["data"]
        assert view["job"]["status"] == "succeeded"
        assert view["job"]["progress"] == {"total": PAGES, "done": PAGES, "failed": 0}
        assert len(view["results"]) <= 10
        urls += [result["url"] for result in view["results"]]
        cursor, calls = view["next_cursor"], calls + 1
        if view["complete"]:
            break
    assert urls == [f"https://example.test/{i}" for i in range(1, PAGES + 1)]
    assert calls == 3


def test_results_of_a_running_job_are_not_complete(client: TestClient) -> None:
    job_id = submit(client, JobKind.HARVEST)
    time.sleep(0.2)
    view = client.get(f"/v1/jobs/{job_id}").json()["data"]
    assert view["job"]["status"] == "running"
    assert view["complete"] is False
    client.delete(f"/v1/jobs/{job_id}")


def test_cancel_stops_a_running_job(client: TestClient) -> None:
    job_id = submit(client, JobKind.HARVEST)
    time.sleep(0.2)

    started = time.monotonic()
    response = client.delete(f"/v1/jobs/{job_id}")
    assert time.monotonic() - started < 3
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancelled"

    done = client.get(f"/v1/jobs/{job_id}").json()["data"]["job"]["progress"]["done"]
    time.sleep(0.2)
    view = client.get(f"/v1/jobs/{job_id}", params={"limit": 1000}).json()["data"]
    assert view["job"]["progress"]["done"] == done  # nothing more after cancelling
    assert view["complete"] is True


def test_cancel_a_finished_job_changes_nothing(client: TestClient) -> None:
    job_id = submit(client, JobKind.BULK)
    wait(client, job_id)
    response = client.delete(f"/v1/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "succeeded"


def test_retain_none_deletes_returned_results(client: TestClient) -> None:
    job_id = submit(client, JobKind.BULK, retain=Retention.NONE)
    wait(client, job_id)
    first = client.get(f"/v1/jobs/{job_id}", params={"limit": 10}).json()["data"]
    assert len(first["results"]) == 10
    # Results already returned cannot be read again, even from the start.
    again = client.get(f"/v1/jobs/{job_id}", params={"limit": 10}).json()["data"]
    assert again["results"][0]["seq"] == 11


@pytest.mark.parametrize("method", ["get", "delete"])
def test_unknown_job(client: TestClient, method: str) -> None:
    response = client.request(method.upper(), "/v1/jobs/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_invalid_limit(client: TestClient) -> None:
    response = client.get("/v1/jobs/any", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def parse_sse(text: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if line and not line.startswith(":")
        )
        if fields:
            events.append(fields)
    return events


def test_event_stream(client: TestClient) -> None:
    job_id = submit(client, JobKind.BULK)
    with client.stream("GET", f"/v1/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.read().decode())

    assert events[0]["event"] == "job"
    results = [event for event in events if event["event"] == "result"]
    assert [int(event["id"]) for event in results] == list(range(1, PAGES + 1))
    assert [json.loads(event["data"])["seq"] for event in results] == list(range(1, PAGES + 1))
    assert json.loads(events[-1]["data"])["status"] == "succeeded"


def test_event_stream_resumes_with_last_event_id(client: TestClient) -> None:
    job_id = submit(client, JobKind.BULK)
    wait(client, job_id)
    response = client.get(f"/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "20"})
    events = parse_sse(response.text)
    assert events[0]["event"] == "job" and events[0]["id"] == "20"
    assert [int(event["id"]) for event in events[1:]] == list(range(21, PAGES + 1))


def test_event_stream_of_unknown_job(client: TestClient) -> None:
    response = client.get("/v1/jobs/nope/events")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
