from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from progdata_crawler.api.rest import create_app
from progdata_crawler.settings import Settings
from tests.support.site_server import SiteServer


@pytest.fixture
def client(tmp_path: Path, test_site: SiteServer) -> Iterator[TestClient]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    with TestClient(create_app(settings, store_path=":memory:")) as test_client:
        yield test_client


def test_fetch(client: TestClient, test_site: SiteServer) -> None:
    response = client.post("/v1/fetch", json={"url": test_site.url("/article.html")})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta", "error"}
    assert body["error"] is None
    assert body["data"]["markdown"].startswith("# How we bake our sourdough")
    assert body["data"]["metadata"]["status"] == 200
    assert response.headers["X-Request-ID"] == body["meta"]["request_id"]


def test_request_id_is_passed_through(client: TestClient, test_site: SiteServer) -> None:
    response = client.post(
        "/v1/fetch", json={"url": test_site.url("/")}, headers={"X-Request-ID": "abc-123"}
    )
    assert response.json()["meta"]["request_id"] == "abc-123"
    assert response.headers["X-Request-ID"] == "abc-123"


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/private/staff.html", 403, "robots_disallowed"),
        ("/status/403", 502, "blocked_by_site"),
        ("/robots.txt", 415, "unsupported_type"),
    ],
)
def test_errors_use_envelope_and_status(
    client: TestClient, test_site: SiteServer, path: str, status: int, code: str
) -> None:
    response = client.post("/v1/fetch", json={"url": test_site.url(path)})
    assert response.status_code == status
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == code


def test_rate_limited_sets_retry_after(client: TestClient, test_site: SiteServer) -> None:
    response = client.post("/v1/fetch", json={"url": test_site.url("/status/429?retry_after=7")})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["Retry-After"] == "7"


def test_timeout(client: TestClient, test_site: SiteServer) -> None:
    response = client.post(
        "/v1/fetch", json={"url": test_site.url("/slow?ms=2000"), "timeout_ms": 300}
    )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "fetch_timeout"


@pytest.mark.parametrize(
    ("payload", "message_start"),
    [
        ({"url": "not a url"}, "url:"),
        ({}, "url:"),
        ({"url": "https://example.com/", "outputs": ["pdf"]}, "outputs.0:"),
        ({"url": "https://example.com/", "cache_tll": 5}, "cache_tll:"),
    ],
)
def test_invalid_requests(
    client: TestClient, payload: dict[str, object], message_start: str
) -> None:
    response = client.post("/v1/fetch", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["message"].startswith(message_start)


def test_openapi_documents_fetch(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/fetch"]["post"]
    assert set(operation["responses"]) >= {"200", "403", "415", "422", "429", "502", "504"}
    assert "FetchRequest" in schema["components"]["schemas"]


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_internal_addresses_are_refused_when_protected(
    tmp_path: Path, test_site: SiteServer
) -> None:
    settings = Settings(data_dir=tmp_path, allow_private_networks=False)
    with TestClient(create_app(settings, store_path=":memory:")) as protected:
        response = protected.post("/v1/fetch", json={"url": test_site.url("/")})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "address_not_allowed"


def test_discover(client: TestClient, test_site: SiteServer) -> None:
    response = client.post(
        "/v1/discover", json={"url": test_site.url("/"), "paths": {"include": ["^/docs/"]}}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    urls = [item["url"] for item in body["data"]["urls"]]
    assert test_site.url("/docs/faq.html") in urls
    assert all("/docs/" in url for url in urls)


def test_discover_invalid_regex(client: TestClient, test_site: SiteServer) -> None:
    response = client.post(
        "/v1/discover", json={"url": test_site.url("/"), "paths": {"include": ["("]}}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_query(client: TestClient, test_site: SiteServer) -> None:
    client.post("/v1/fetch", json={"url": test_site.url("/article.html")})
    response = client.post("/v1/query", json={"query": "sourdough"})
    assert response.status_code == 200
    hits = response.json()["data"]["hits"]
    assert [hit["url"] for hit in hits] == [test_site.url("/article.html")]
    assert client.post("/v1/query", json={"query": ""}).status_code == 422
