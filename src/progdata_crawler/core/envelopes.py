"""Wrapping core operations in the response envelope every entry point returns."""

import logging
import time
import uuid
from collections.abc import Awaitable

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import Envelope, ErrorCode, ErrorInfo, Meta

log = logging.getLogger(__name__)


async def enveloped[T](
    result_type: type[T], operation: Awaitable[T], *, request_id: str | None = None
) -> Envelope[T]:
    """Run `operation`. Its result becomes `data`; a `CrawlerError` becomes `error`."""
    request_id = request_id or uuid.uuid4().hex
    started = time.monotonic()
    data: T | None = None
    error: ErrorInfo | None = None
    try:
        data = await operation
    except CrawlerError as exc:
        error = exc.to_error_info()
    except Exception:
        log.exception("unexpected error", extra={"request_id": request_id})
        error = ErrorInfo(code=ErrorCode.INTERNAL_ERROR, message="unexpected internal error")
    meta = Meta(request_id=request_id, took_ms=round((time.monotonic() - started) * 1000))
    return Envelope[result_type](data=data, meta=meta, error=error)
