"""POST /v1/structure: extract data from one or more pages against a schema."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from progdata_crawler.models.bulk import MAX_BULK_URLS
from progdata_crawler.models.jobs import JobView
from progdata_crawler.models.options import Output, PageOptions


class StructureRequest(PageOptions):
    """Extract `structure` from every URL. One URL is answered directly; several run as
    a job, followed like a bulk job."""

    urls: tuple[HttpUrl, ...] = Field(min_length=1, max_length=MAX_BULK_URLS)
    outputs: tuple[Output, ...] = Field(default=(Output.STRUCTURED,), min_length=1)

    @model_validator(mode="after")
    def _asks_for_structured(self) -> "StructureRequest":
        if self.structure is None:
            raise ValueError("structure is required: a schema and an instruction")
        if Output.STRUCTURED not in self.outputs:
            raise ValueError("outputs must include 'structured'")
        return self


class StructureResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str | None = None
    """For one URL: the page the data came from (after redirects)."""
    data: dict[str, Any] | None = None
    """For one URL: the extracted data."""
    job: JobView | None = None
    """For several URLs: the job, with the results so far."""
