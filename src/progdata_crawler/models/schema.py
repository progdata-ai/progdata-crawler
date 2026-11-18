"""JSON Schema export for the public models."""

from typing import Any

from pydantic import BaseModel

from progdata_crawler.models.bulk import BulkRequest
from progdata_crawler.models.discover import DiscoveredUrl, DiscoverRequest, DiscoverResult
from progdata_crawler.models.envelope import ErrorInfo, Meta
from progdata_crawler.models.fetch import FetchRequest
from progdata_crawler.models.harvest import HarvestRequest
from progdata_crawler.models.options import PageOptions, PathFilter, SelectorFilter
from progdata_crawler.models.page import Image, Link, Page, PageMetadata
from progdata_crawler.models.query import QueryHit, QueryRequest, QueryResult

PUBLIC_MODELS: tuple[type[BaseModel], ...] = (
    FetchRequest,
    QueryRequest,
    QueryResult,
    QueryHit,
    BulkRequest,
    HarvestRequest,
    PageOptions,
    SelectorFilter,
    PathFilter,
    Meta,
    ErrorInfo,
    Page,
    PageMetadata,
    Link,
    Image,
    DiscoverRequest,
    DiscoverResult,
    DiscoveredUrl,
)


def json_schemas() -> dict[str, dict[str, Any]]:
    """Return the JSON Schema of every public model, keyed by model name."""
    return {model.__name__: model.model_json_schema() for model in PUBLIC_MODELS}
