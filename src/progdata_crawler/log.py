"""Logging setup.

Code logs with the standard library (`logging.getLogger(__name__)`) and passes context
as `extra={...}`. `configure_logging` decides how the records are written.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO, override

from progdata_crawler.settings import LogFormat, LogLevel

ROOT_LOGGER = "progdata_crawler"

# Attributes every LogRecord has. Anything else on a record came from `extra`.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in vars(record).items() if key not in _STANDARD_ATTRS}


class JsonFormatter(logging.Formatter):
    """Writes each record as one JSON object, including fields passed as `extra`."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_extra_fields(record),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Readable lines with `key=value` pairs for fields passed as `extra`."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    @override
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = _extra_fields(record)
        if extra:
            line += " " + " ".join(f"{key}={value}" for key, value in extra.items())
        return line


def configure_logging(
    level: LogLevel = LogLevel.INFO,
    fmt: LogFormat = LogFormat.TEXT,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Send the package's log records to `stream` (stderr by default).

    Safe to call more than once: earlier handlers from this function are replaced.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt is LogFormat.JSON else TextFormatter())

    logger = logging.getLogger(ROOT_LOGGER)
    for old in list(logger.handlers):
        logger.removeHandler(old)
    logger.addHandler(handler)
    logger.setLevel(level.value)
    logger.propagate = False
    return logger
