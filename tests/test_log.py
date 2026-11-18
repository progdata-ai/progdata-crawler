import io
import json
import logging

from progdata_crawler.log import configure_logging
from progdata_crawler.settings import LogFormat, LogLevel


def test_json_format_includes_extra_fields() -> None:
    stream = io.StringIO()
    configure_logging(LogLevel.INFO, LogFormat.JSON, stream)

    logging.getLogger("progdata_crawler.core.fetch").info(
        "page fetched", extra={"url": "https://example.com/", "status": 200}
    )

    record = json.loads(stream.getvalue())
    assert record["level"] == "INFO"
    assert record["logger"] == "progdata_crawler.core.fetch"
    assert record["message"] == "page fetched"
    assert record["url"] == "https://example.com/"
    assert record["status"] == 200
    assert "time" in record


def test_json_format_includes_exceptions() -> None:
    stream = io.StringIO()
    configure_logging(LogLevel.INFO, LogFormat.JSON, stream)

    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("progdata_crawler").exception("failed")

    record = json.loads(stream.getvalue())
    assert "ValueError: boom" in record["exception"]


def test_text_format_appends_extra_fields() -> None:
    stream = io.StringIO()
    configure_logging(LogLevel.INFO, LogFormat.TEXT, stream)

    logging.getLogger("progdata_crawler").warning("slow page", extra={"took_ms": 4200})

    line = stream.getvalue().strip()
    assert "WARNING" in line
    assert "slow page" in line
    assert line.endswith("took_ms=4200")


def test_level_filters_records() -> None:
    stream = io.StringIO()
    configure_logging(LogLevel.WARNING, LogFormat.TEXT, stream)

    logger = logging.getLogger("progdata_crawler")
    logger.info("hidden")
    logger.warning("shown")

    assert "hidden" not in stream.getvalue()
    assert "shown" in stream.getvalue()


def test_reconfiguring_replaces_the_handler() -> None:
    first, second = io.StringIO(), io.StringIO()
    configure_logging(LogLevel.INFO, LogFormat.TEXT, first)
    configure_logging(LogLevel.INFO, LogFormat.TEXT, second)

    logging.getLogger("progdata_crawler").info("once")

    assert first.getvalue() == ""
    assert second.getvalue().count("once") == 1
