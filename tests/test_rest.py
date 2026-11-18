from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from progdata_crawler.api.rest import create_app
from progdata_crawler.models import SearchSource
from progdata_crawler.settings import Settings
from tests.support.fake_search import FakeSearch, web_hits
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


def test_find_fetches_the_hits(tmp_path: Path, test_site: SiteServer) -> None:
    test_site.add("/robots.txt", "User-agent: *\n", content_type="text/plain")
    provider = FakeSearch(
        {SearchSource.WEB: web_hits(test_site.url("/article.html"), test_site.url("/docs/"))}
    )
    settings = Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)
    app = create_app(settings, store_path=":memory:", search_provider=provider)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/find", json={"query": "baking", "fetch": {"outputs": ["markdown"]}}
        )
    assert provider.closed
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["provider"] == "fake"
    markdown = [hit["page"]["markdown"] for hit in data["hits"]]
    assert markdown[0].startswith("# How we bake our sourdough")
    assert len(markdown) == 2 and all(markdown)


def test_find_without_a_configured_provider(client: TestClient) -> None:
    response = client.post("/v1/find", json={"query": "baking"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "search_unavailable"


def test_browser_session(client: TestClient, test_site: SiteServer) -> None:
    test_site.add("/submit", "<html><body><h1>Thank you for your order</h1></body></html>")
    opened = client.post("/v1/session", json={"url": test_site.url("/form.html")})
    assert opened.status_code == 200
    session_id = opened.json()["data"]["id"]
    step = client.post(
        f"/v1/session/{session_id}/step",
        json={
            "steps": [
                {"action": "type", "selector": "#name", "text": "Ada"},
                {"action": "click", "selector": "#send"},
            ]
        },
    )
    assert step.json()["data"]["page"]["markdown"].startswith("# Thank you for your order")
    missing = client.post(
        f"/v1/session/{session_id}/step",
        json={"steps": [{"action": "click", "selector": "#none", "timeout_ms": 200}]},
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "step_failed"
    assert client.delete(f"/v1/session/{session_id}").json()["data"]["closed"] is True
    gone = client.delete(f"/v1/session/{session_id}")
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "not_found")


def test_watch_endpoints(client: TestClient, test_site: SiteServer) -> None:
    added = client.post(
        "/v1/watch", json={"url": test_site.url("/article.html"), "interval_s": 600}
    )
    assert added.status_code == 200
    watch_id = added.json()["data"]["id"]
    check = client.post(f"/v1/watch/{watch_id}/check").json()["data"]
    assert check["status"] in ("first", "unchanged")
    listed = client.get("/v1/watch").json()["data"]["watches"]
    assert [w["id"] for w in listed] == [watch_id]
    assert client.get(f"/v1/watch/{watch_id}").json()["data"]["url"] == test_site.url(
        "/article.html"
    )
    assert client.delete(f"/v1/watch/{watch_id}").status_code == 200
    gone = client.get(f"/v1/watch/{watch_id}")
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "not_found")
    refused = client.post("/v1/watch", json={"url": test_site.url("/"), "retain": "none"})
    assert refused.status_code == 422
