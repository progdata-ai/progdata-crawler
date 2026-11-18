"""Convert: documents (PDF, DOCX, XLSX) to markdown."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, HttpUrl


class DocumentType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"


class ConvertRequest(BaseModel):
    """Convert the document at `url`. Uploaded files are converted without a request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl


class ConvertResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    """The URL, or the uploaded file's name."""
    type: DocumentType
    markdown: str
    title: str | None = None
    """The document's title, when it has one."""
