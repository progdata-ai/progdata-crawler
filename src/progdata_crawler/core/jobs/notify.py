"""Webhooks: POST to a job's `notify_url` when its status changes.

The body is signed with HMAC-SHA256 over `<timestamp>.<body>` using the configured
secret (`PDC_WEBHOOK_SECRET`). Receivers check the signature and reject old timestamps,
so a captured request cannot be replayed later. Requests go through the address guard,
redirects are not followed, and failed deliveries are retried with backoff within a
time limit. The webhook carries the job's status, never page content.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.core.fetch.guard import AddressGuard, GuardedTransport
from progdata_crawler.models.jobs import Job, JobStatus

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-PDC-Signature"
TIMESTAMP_HEADER = "X-PDC-Timestamp"
EVENT_HEADER = "X-PDC-Event"

RETRY_DELAYS_S: tuple[float, ...] = (1, 2, 4, 8, 16)
"""Waits between attempts. One attempt more than there are delays."""
DEADLINE_S = 60.0
"""No attempts start after this many seconds."""
TIMEOUT_S = 10.0
"""Time limit for one attempt."""


def event_name(status: JobStatus) -> str:
    return "job.started" if status is JobStatus.RUNNING else f"job.{status.value}"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The signature header value for `body` sent at `timestamp` (Unix seconds)."""
    message = str(timestamp).encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    secret: str, timestamp: int, body: bytes, signature: str, *, max_age_s: int = 300
) -> bool:
    """Check a signature the way a receiver should: constant-time, and not too old."""
    if abs(time.time() - timestamp) > max_age_s:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


class WebhookNotifier:
    """Sends webhooks in the background, so a slow receiver never holds up a job."""

    def __init__(
        self,
        secret: str | None,
        user_agent: str,
        guard: AddressGuard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delays_s: Sequence[float] = RETRY_DELAYS_S,
        deadline_s: float = DEADLINE_S,
    ) -> None:
        self._secret = secret
        self._guard = guard
        self._retry_delays_s = tuple(retry_delays_s)
        self._deadline_s = deadline_s
        guarded = not guard.allow_private
        if transport is None and guarded:
            transport = GuardedTransport(guard)
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=not guarded,
            follow_redirects=False,
            timeout=TIMEOUT_S,
            headers={"User-Agent": user_agent, "Content-Type": "application/json"},
        )
        self._tasks: set[asyncio.Task[bool]] = set()

    @property
    def enabled(self) -> bool:
        return self._secret is not None

    def notify(self, job: Job) -> None:
        """Send the job's current status to its `notify_url`, if it has one."""
        url = job.request.get("notify_url")
        if not isinstance(url, str) or self._secret is None:
            return
        task = asyncio.create_task(self.deliver(url, job), name=f"webhook-{job.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def deliver(self, url: str, job: Job) -> bool:
        """Send one webhook, with retries. True when the receiver answered 2xx."""
        assert self._secret is not None
        event = event_name(job.status)
        body = json.dumps(
            {"event": event, "job": job.model_dump(mode="json", exclude={"request"})},
            separators=(",", ":"),
        ).encode()
        context = {"job_id": job.id, "event": event, "host": urlsplit(url).hostname}
        started = time.monotonic()
        for attempt in range(len(self._retry_delays_s) + 1):
            if attempt:
                delay = self._retry_delays_s[attempt - 1]
                if time.monotonic() - started + delay > self._deadline_s:
                    break
                await asyncio.sleep(delay)
            timestamp = int(time.time())
            headers = {
                EVENT_HEADER: event,
                TIMESTAMP_HEADER: str(timestamp),
                SIGNATURE_HEADER: sign(self._secret, timestamp, body),
            }
            try:
                await self._guard.check_url(url)
                response = await self._client.post(url, content=body, headers=headers)
            except CrawlerError as error:
                log.warning("webhook refused", extra={**context, "code": error.code.value})
                return False  # an internal address stays internal: no retry
            except httpx.HTTPError as error:
                log.info(
                    "webhook attempt failed",
                    extra={**context, "attempt": attempt + 1, "error": type(error).__name__},
                )
                continue
            if response.is_success:
                return True
            log.info(
                "webhook attempt rejected",
                extra={**context, "attempt": attempt + 1, "status": response.status_code},
            )
            if response.status_code < 500 and response.status_code != 429:
                return False  # the receiver does not want it; retrying will not help
        log.warning("webhook gave up", extra=context)
        return False

    async def aclose(self, *, wait_s: float = 5.0) -> None:
        """Give deliveries in flight a moment to finish, then stop them."""
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=wait_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self._client.aclose()
