"""The exception the core raises for every expected failure.

Entry points catch `CrawlerError` and turn it into their own format: an error envelope
for REST and MCP, a message and exit code for the CLI.
"""

from typing import Any

from progdata_crawler.models.envelope import ErrorCode, ErrorInfo


class CrawlerError(Exception):
    """An expected failure with a stable error code."""

    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_error_info(self) -> ErrorInfo:
        return ErrorInfo(code=self.code, message=self.message, details=self.details)
