import json

from progdata_crawler.models import Envelope, Meta, json_schemas


def test_exports_a_schema_for_every_public_model() -> None:
    schemas = json_schemas()
    assert set(schemas) == {
        "FetchRequest",
        "HarvestRequest",
        "PageOptions",
        "SelectorFilter",
        "PathFilter",
        "Meta",
        "ErrorInfo",
        "Page",
        "PageMetadata",
        "Link",
        "Image",
        "DiscoverRequest",
        "DiscoverResult",
        "DiscoveredUrl",
    }
    for schema in schemas.values():
        assert schema["type"] == "object"
        json.dumps(schema)  # serializable


def test_fetch_request_schema_documents_the_options() -> None:
    schema = json_schemas()["FetchRequest"]
    assert schema["required"] == ["url"]
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["url"]["format"] == "uri"
    assert properties["cache_ttl"]["default"] == 86_400
    assert properties["outputs"]["default"] == ["markdown", "metadata"]


def test_parametrized_envelope_has_a_schema() -> None:
    schema = Envelope[Meta].model_json_schema()
    assert set(schema["properties"]) == {"data", "meta", "error"}
