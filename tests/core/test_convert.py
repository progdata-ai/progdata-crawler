import io
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from progdata_crawler.core.convert import documents
from progdata_crawler.core.convert.documents import convert_bytes, convert_url, detect_type
from progdata_crawler.core.crawler import Crawler
from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models import ConvertResult, DocumentType, ErrorCode
from progdata_crawler.settings import Settings
from tests.support.documents import build_docx, build_xlsx
from tests.support.site_server import PDF_TEXT, SiteServer, build_pdf

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
async def crawler(tmp_path: Path, test_site: SiteServer) -> AsyncIterator[Crawler]:
    test_site.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    async with Crawler(Settings(data_dir=tmp_path, rate_limit_interval_s=0.1)) as instance:
        yield instance


async def test_pdf() -> None:
    result = await convert_bytes(build_pdf(), source="notes.pdf", name="notes.pdf")
    assert result == ConvertResult(source="notes.pdf", type=DocumentType.PDF, markdown=PDF_TEXT)


async def test_docx() -> None:
    result = await convert_bytes(build_docx(), source="notes.docx", content_type=DOCX)
    assert result.type is DocumentType.DOCX
    assert result.markdown == (
        "# Baking notes\n\nKeep the starter **warm** and feed it daily.\n\n"
        "Rye ferments faster than wheat."
    )


async def test_xlsx() -> None:
    result = await convert_bytes(build_xlsx(), source="recipe.xlsx")
    assert result.type is DocumentType.XLSX
    assert result.markdown == (
        "## Recipe\n| Flour | Grams |\n| --- | --- |\n| Rye | 300 |\n| Wheat | 700 |"
    )


def test_type_comes_from_the_bytes() -> None:
    assert detect_type(build_pdf()) is DocumentType.PDF
    assert detect_type(build_docx(), content_type="application/octet-stream") is DocumentType.DOCX
    assert detect_type(build_xlsx(), name="data.bin") is DocumentType.XLSX
    # A name or content type that does not match the bytes is not trusted.
    assert detect_type(build_pdf(), name="report.docx") is None
    assert detect_type(build_docx(), content_type=XLSX) is None
    assert detect_type(b"<html></html>", name="page.pdf") is None
    other_zip = io.BytesIO()
    with zipfile.ZipFile(other_zip, "w") as archive:
        archive.writestr("readme.txt", "hello")
    assert detect_type(other_zip.getvalue()) is None


@pytest.mark.parametrize(
    "content",
    [b"just text", b"%PDF-1.4\nbroken beyond repair", b"PK\x03\x04 not really a zip"],
)
async def test_other_or_damaged_files(content: bytes) -> None:
    with pytest.raises(CrawlerError) as raised:
        await convert_bytes(content, source="x")
    assert raised.value.code is ErrorCode.UNSUPPORTED_TYPE


async def test_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "MAX_DOCUMENT_BYTES", 100)
    with pytest.raises(CrawlerError) as raised:
        await convert_bytes(build_pdf(), source="big.pdf")
    assert raised.value.details["max_bytes"] == 100


async def test_zip_bombs_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "MAX_UNPACKED_BYTES", 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        with zipfile.ZipFile(io.BytesIO(build_docx())) as source:
            for item in source.infolist():
                archive.writestr(item, source.read(item))
        archive.writestr("word/media/padding.bin", b"\0" * (2 * 1024 * 1024))
    bomb = buffer.getvalue()
    assert len(bomb) < 100_000
    with pytest.raises(CrawlerError) as raised:
        await convert_bytes(bomb, source="bomb.docx")
    assert raised.value.code is ErrorCode.UNSUPPORTED_TYPE
    assert "unpacked" in raised.value.message


async def test_from_url(crawler: Crawler, test_site: SiteServer) -> None:
    pdf = await convert_url(crawler, test_site.url("/files/sample.pdf"))
    assert (pdf.type, pdf.markdown, pdf.source) == (
        DocumentType.PDF,
        PDF_TEXT,
        test_site.url("/files/sample.pdf"),
    )
    url = test_site.add("/files/notes", build_docx(), content_type="application/octet-stream")
    docx = await convert_url(crawler, url)
    assert docx.type is DocumentType.DOCX


async def test_from_url_is_polite(crawler: Crawler, test_site: SiteServer) -> None:
    test_site.add("/private/secret.pdf", build_pdf(), content_type="application/pdf")
    with pytest.raises(CrawlerError) as raised:
        await convert_url(crawler, test_site.url("/private/secret.pdf"))
    assert raised.value.code is ErrorCode.ROBOTS_DISALLOWED

    with pytest.raises(CrawlerError) as raised:
        await convert_url(crawler, test_site.url("/article.html"))
    assert raised.value.code is ErrorCode.UNSUPPORTED_TYPE


async def test_from_url_refuses_internal_addresses(tmp_path: Path, test_site: SiteServer) -> None:
    settings = Settings(data_dir=tmp_path, allow_private_networks=False)
    async with Crawler(settings) as crawler:
        with pytest.raises(CrawlerError) as raised:
            await convert_url(crawler, test_site.url("/files/sample.pdf"))
    assert raised.value.code is ErrorCode.ADDRESS_NOT_ALLOWED
