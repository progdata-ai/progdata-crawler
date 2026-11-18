"""Contract tests for POST /v1/structure: one URL directly, several as a job."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings
from tests.support.fake_llm import FakeLlm
from tests.support.site_server import SiteServer

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"heading": {"type": "string"}},
    "required": ["heading"],
}


def heading_of(prompt: str) -> dict[str, Any]:
    for line in prompt.splitlines():
        if line.startswith("# "):
            return {"heading": line[2:]}
    return {"heading": "?"}


@pytest.fixture
def client(tmp_path: Path, test_site: SiteServer) -> Iterator[TestClient]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    app = create_app(settings, store_path=":memory:", llm=FakeLlm(heading_of).llm())
    with TestClient(app) as test_client:
        yield test_client


def body(*urls: str, **extra: Any) -> dict[str, Any]:
    return {
        "urls": list(urls),
        "structure": {"schema": SCHEMA, "instruction": "the heading"},
        **extra,
    }


def test_one_url_answers_directly(client: TestClient, test_site: SiteServer) -> None:
    response = client.post("/v1/structure", json=body(test_site.url("/article.html")))
    assert response.status_code == 200
    envelope = response.json()
    assert set(envelope) == {"data", "meta", "error"}
    assert envelope["data"] == {
        "url": test_site.url("/article.html"),
        "data": {"heading": "How we bake our sourdough"},
        "job": None,
    }


def test_several_urls_run_as_a_job(client: TestClient, test_site: SiteServer) -> None:
    urls = [
        test_site.url("/article.html"),
        test_site.url("/docs/"),
        test_site.url("/private/staff.html"),
    ]
    response = client.post("/v1/structure", json=body(*urls), params={"wait_s": 60})
    assert response.status_code == 200
    result = response.json()["data"]
    assert (result["url"], result["data"]) == (None, None)
    job = result["job"]
    assert job["job"]["kind"] == "bulk" and job["job"]["status"] == "succeeded"
    assert job["complete"] is True
    by_url = {item["url"]: item for item in job["results"]}
    assert by_url[test_site.url("/article.html")]["page"]["structured"] == {
        "heading": "How we bake our sourdough"
    }
    assert by_url[test_site.url("/private/staff.html")]["error"]["code"] == "robots_disallowed"

    followed = client.get(f"/v1/jobs/{job['job']['id']}").json()["data"]
    assert len(followed["results"]) == 3


def test_several_urls_without_waiting(client: TestClient, test_site: SiteServer) -> None:
    urls = [test_site.url("/article.html"), test_site.url("/docs/")]
    job = client.post("/v1/structure", json=body(*urls)).json()["data"]["job"]
    assert job["job"]["status"] in ("queued", "running", "succeeded")


def test_extraction_that_fails(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    llm = FakeLlm(lambda prompt: {"wrong": 1}).llm()
    with TestClient(
        create_app(Settings(data_dir=tmp_path), store_path=":memory:", llm=llm)
    ) as client:
        response = client.post("/v1/structure", json=body(test_site.url("/article.html")))
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "extraction_failed"
    assert "'heading' is a required property" in error["message"]


def test_without_an_llm(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    with TestClient(create_app(Settings(data_dir=tmp_path), store_path=":memory:")) as client:
        response = client.post("/v1/structure", json=body(test_site.url("/article.html")))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"


@pytest.mark.parametrize(
    "payload",
    [
        {"urls": ["https://example.test/"]},
        {"urls": [], "structure": {"schema": SCHEMA}},
        {"urls": ["https://example.test/"], "structure": {"schema": {"type": "array"}}},
        {
            "urls": ["https://example.test/"],
            "structure": {"schema": SCHEMA},
            "outputs": ["markdown"],
        },
    ],
)
def test_invalid_requests(client: TestClient, payload: dict[str, Any]) -> None:
    response = client.post("/v1/structure", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_documented_in_openapi(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/v1/structure"]["post"]
    assert "requestBody" in operation
    assert {"200", "422", "502", "503"} <= set(operation["responses"])
    assert "StructureRequest" in spec["components"]["schemas"]
