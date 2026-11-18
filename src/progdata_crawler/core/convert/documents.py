"""PDF, DOCX and XLSX to markdown.

The conversion itself is done by markitdown, with only the three converters we need.
markitdown can also fetch URLs on its own; we never let it. Documents from the web are
fetched by the `Crawler` (address guard, robots.txt, rate limit) and handed over as bytes.
"""

import asyncio
import io
import logging
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from markitdown import MarkItDown, StreamInfo
from markitdown.converters import DocxConverter, PdfConverter, XlsxConverter

from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.convert import ConvertResult, DocumentType
from progdata_crawler.models.envelope import ErrorCode

log = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
"""Largest document we convert."""

MAX_UNPACKED_BYTES = 200 * 1024 * 1024
"""Largest total size of the files inside a DOCX or XLSX (both are zip archives)."""

CONTENT_TYPES: dict[str, DocumentType] = {
    "application/pdf": DocumentType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocumentType.DOCX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentType.XLSX,
}
"""Content types that are documents, by their media type."""

GENERIC_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
"""Content types servers often send for any file; the content decides then."""

_EXTENSIONS = {"." + kind.value: kind for kind in DocumentType}
_MIMETYPES = {kind: mimetype for mimetype, kind in CONTENT_TYPES.items()}


def _sniff(content: bytes) -> DocumentType | None:
    """The type from the bytes themselves."""
    if content.startswith(b"%PDF-"):
        return DocumentType.PDF
    if not content.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return DocumentType.DOCX
    if "xl/workbook.xml" in names:
        return DocumentType.XLSX
    return None


def detect_type(
    content: bytes, *, content_type: str | None = None, name: str | None = None
) -> DocumentType | None:
    """The document type, from the content type, the file name or the bytes. The bytes
    must agree: a file called .pdf that is not a PDF is not converted as one."""
    sniffed = _sniff(content)
    claimed = CONTENT_TYPES.get(content_type or "")
    if claimed is None and name:
        claimed = _EXTENSIONS.get(PurePosixPath(name.lower()).suffix)
    if claimed is not None and sniffed is not None and claimed is not sniffed:
        return None
    return sniffed


def _check_archive(content: bytes) -> None:
    """Refuse zip bombs: archives whose files unpack to far more than they weigh."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        unpacked = sum(info.file_size for info in archive.infolist())
    if unpacked > MAX_UNPACKED_BYTES:
        raise CrawlerError(
            ErrorCode.UNSUPPORTED_TYPE,
            "the document is too large when unpacked",
            max_bytes=MAX_UNPACKED_BYTES,
        )


def _converter() -> MarkItDown:
    converter = MarkItDown(enable_builtins=False, enable_plugins=False)
    for document_converter in (PdfConverter(), DocxConverter(), XlsxConverter()):
        converter.register_converter(document_converter)
    return converter


def _convert(content: bytes, kind: DocumentType) -> tuple[str, str | None]:
    if kind is not DocumentType.PDF:
        _check_archive(content)
    info = StreamInfo(mimetype=_MIMETYPES[kind], extension="." + kind.value)
    try:
        result = _converter().convert_stream(io.BytesIO(content), stream_info=info)
    except Exception as error:  # markitdown raises many kinds on damaged files
        log.info(
            "document conversion failed", extra={"type": kind.value, "error": type(error).__name__}
        )
        raise CrawlerError(
            ErrorCode.UNSUPPORTED_TYPE, f"the {kind.value.upper()} file could not be read"
        ) from error
    return result.markdown.strip(), result.title


async def convert_bytes(
    content: bytes,
    *,
    source: str,
    content_type: str | None = None,
    name: str | None = None,
) -> ConvertResult:
    """Convert a document to markdown. Raises `unsupported_type` for other files, damaged
    documents and documents over the size limits."""
    if len(content) > MAX_DOCUMENT_BYTES:
        raise CrawlerError(
            ErrorCode.UNSUPPORTED_TYPE,
            f"the document is larger than {MAX_DOCUMENT_BYTES} bytes",
            max_bytes=MAX_DOCUMENT_BYTES,
        )
    kind = detect_type(content, content_type=content_type, name=name)
    if kind is None:
        raise CrawlerError(
            ErrorCode.UNSUPPORTED_TYPE,
            "not a PDF, DOCX or XLSX document",
            content_type=content_type or "unknown",
        )
    markdown, title = await asyncio.to_thread(_convert, content, kind)
    return ConvertResult(source=source, type=kind, markdown=markdown, title=title or None)


async def convert_url(crawler: Crawler, url: str) -> ConvertResult:
    """Fetch the document at `url` politely and convert it."""
    response = await crawler.fetch_document(
        url,
        accept_types=frozenset(CONTENT_TYPES) | GENERIC_TYPES,
        max_bytes=MAX_DOCUMENT_BYTES,
    )
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    name = PurePosixPath(urlsplit(response.url).path).name or None
    return await convert_bytes(
        response.content, source=response.url, content_type=content_type, name=name
    )
