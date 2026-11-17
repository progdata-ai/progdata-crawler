import pytest
from pydantic import ValidationError

from progdata_crawler.models import (
    DEFAULT_CACHE_TTL_SECONDS,
    ContentMode,
    Device,
    FetchRequest,
    Output,
    PageOptions,
    PathFilter,
    RenderMode,
    Retention,
)


def test_defaults() -> None:
    options = PageOptions()
    assert options.outputs == (Output.MARKDOWN, Output.METADATA)
    assert options.content is ContentMode.MAIN
    assert options.device is Device.DESKTOP
    assert options.render is RenderMode.AUTO
    assert options.retain is Retention.DEFAULT
    assert options.cache_ttl == DEFAULT_CACHE_TTL_SECONDS == 86_400
    assert options.tags.include == ()
    assert options.tags.exclude == ()


def test_parses_json_values() -> None:
    options = PageOptions.model_validate(
        {
            "outputs": ["markdown", "links", "screenshot"],
            "content": "full",
            "tags": {"include": ["article"], "exclude": ["nav", ".ads"]},
            "wait_ms": 500,
            "device": "mobile",
            "locale": "nb-NO",
            "geo": "NO",
            "render": "always",
            "cache_ttl": 0,
            "timeout_ms": 10_000,
            "retain": "none",
        }
    )
    assert options.outputs == (Output.MARKDOWN, Output.LINKS, Output.SCREENSHOT)
    assert options.tags.exclude == ("nav", ".ads")
    assert options.retain is Retention.NONE


@pytest.mark.parametrize(
    "overrides",
    [
        {"outputs": []},
        {"outputs": ["markdown", "markdown"]},
        {"outputs": ["pdf"]},
        {"content": "partial"},
        {"wait_ms": -1},
        {"wait_ms": 30_001},
        {"timeout_ms": 0},
        {"timeout_ms": 120_001},
        {"cache_ttl": -1},
        {"locale": "norwegian bokmål"},
        {"geo": "no"},
        {"tags": {"include": [""]}},
        {"unknown_option": True},
    ],
)
def test_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PageOptions.model_validate(overrides)


def test_options_are_immutable() -> None:
    options = PageOptions()
    with pytest.raises(ValidationError):
        options.wait_ms = 100  # pyright: ignore[reportAttributeAccessIssue]


def test_fetch_request_requires_http_url() -> None:
    request = FetchRequest.model_validate({"url": "https://example.com/docs"})
    assert str(request.url) == "https://example.com/docs"
    assert request.outputs == (Output.MARKDOWN, Output.METADATA)

    for bad in ("ftp://example.com/file", "not a url", ""):
        with pytest.raises(ValidationError):
            FetchRequest.model_validate({"url": bad})
    with pytest.raises(ValidationError):
        FetchRequest.model_validate({})


def test_path_filter_rejects_invalid_regex() -> None:
    assert PathFilter(include=(r"^/docs/",), exclude=(r"\.pdf$",)).include == (r"^/docs/",)
    with pytest.raises(ValidationError, match="invalid regular expression"):
        PathFilter(exclude=("(unclosed",))
