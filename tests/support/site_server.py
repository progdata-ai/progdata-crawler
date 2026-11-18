"""A local website for tests, so no test ever talks to an external site.

Static pages live in `tests/support/site/`. A few routes are generated on the fly:

- `/robots.txt`: disallows `/private/`, sets a crawl delay and points to the sitemap.
- `/sitemap.xml`: lists the public pages with absolute URLs.
- `/slow?ms=N`: answers after N milliseconds.
- `/status/<code>`: answers with that status. 429 and 503 include `Retry-After`
  (seconds, from `?retry_after=`, default 2).
- `/redirect?to=<path>`: 302 redirect to another path on the site.
- `/echo/headers`: the request headers as JSON, to check what a client sends.
- `/files/sample.pdf`: a small, valid PDF with one line of text.

Tests can add their own routes with `SiteServer.add`.
"""

import json
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, override
from urllib.parse import parse_qs, urlsplit

STATIC_DIR = Path(__file__).parent / "site"

PUBLIC_PAGES = (
    "/",
    "/article.html",
    "/js-app.html",
    "/docs/",
    "/docs/getting-started.html",
    "/docs/configuration.html",
    "/docs/faq.html",
)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
}

PDF_TEXT = "Hello from the progdata-crawler test site"


def build_pdf(text: str = PDF_TEXT) -> bytes:
    """Build a minimal, valid one-page PDF that shows `text`."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)


@dataclass(frozen=True)
class Response:
    body: bytes
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict[str, str])


class SiteServer:
    """The running test site. Use `url(path)` to build absolute URLs."""

    def __init__(self, server: ThreadingHTTPServer) -> None:
        self._server = server
        host, port = server.server_address[:2]
        self.base_url = f"http://{host!s}:{port}"
        self._routes: dict[str, Response] = {}
        self._lock = threading.Lock()

    def url(self, path: str = "/") -> str:
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def add(
        self,
        path: str,
        body: str | bytes,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> str:
        """Serve `body` at `path` until `reset()` is called. Returns the absolute URL."""
        data = body.encode() if isinstance(body, str) else body
        with self._lock:
            self._routes[path] = Response(data, status, content_type, dict(headers or {}))
        return self.url(path)

    def reset(self) -> None:
        """Remove the routes added with `add`."""
        with self._lock:
            self._routes.clear()

    def custom_route(self, path: str) -> Response | None:
        with self._lock:
            return self._routes.get(path)


class _Handler(BaseHTTPRequestHandler):
    site: ClassVar[SiteServer]
    protocol_version = "HTTP/1.1"

    @override
    def log_message(self, format: str, *args: Any) -> None:
        """Keep test output quiet."""

    def do_GET(self) -> None:
        self._send(self._route())

    def do_HEAD(self) -> None:
        self._send(self._route(), include_body=False)

    def _route(self) -> Response:
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)

        custom = self.site.custom_route(path)
        if custom is not None:
            return custom
        if path == "/robots.txt":
            return self._robots()
        if path == "/sitemap.xml":
            return self._sitemap()
        if path == "/slow":
            time.sleep(int(query.get("ms", ["1000"])[0]) / 1000)
            return Response(b"<html><body><p>Finally here.</p></body></html>")
        if path.startswith("/status/"):
            return self._status(path.removeprefix("/status/"), query)
        if path == "/redirect":
            target = query.get("to", ["/"])[0]
            return Response(b"", status=302, headers={"Location": self.site.url(target)})
        if path == "/echo/headers":
            body = json.dumps({key.lower(): value for key, value in self.headers.items()})
            return Response(body.encode(), content_type="application/json")
        if path == "/files/sample.pdf":
            return Response(build_pdf(), content_type="application/pdf")
        return self._static(path)

    def _robots(self) -> Response:
        body = (
            "User-agent: *\n"
            "Disallow: /private/\n"
            "Crawl-delay: 1\n"
            "\n"
            f"Sitemap: {self.site.url('/sitemap.xml')}\n"
        )
        return Response(body.encode(), content_type="text/plain; charset=utf-8")

    def _sitemap(self) -> Response:
        entries = "".join(
            f"  <url><loc>{self.site.url(page)}</loc></url>\n" for page in PUBLIC_PAGES
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{entries}</urlset>\n"
        )
        return Response(body.encode(), content_type="application/xml")

    def _status(self, code: str, query: dict[str, list[str]]) -> Response:
        status = int(code) if code.isdigit() else 400
        headers: dict[str, str] = {}
        if status in (429, 503):
            headers["Retry-After"] = query.get("retry_after", ["2"])[0]
        body = f"<html><body><p>Status {status}</p></body></html>".encode()
        return Response(body, status=status, headers=headers)

    def _static(self, path: str) -> Response:
        relative = path.lstrip("/")
        if relative == "" or relative.endswith("/"):
            relative += "index.html"
        file = (STATIC_DIR / relative).resolve()
        if not file.is_relative_to(STATIC_DIR.resolve()) or not file.is_file():
            return Response(b"<html><body><p>Not found</p></body></html>", status=404)
        content_type = _CONTENT_TYPES.get(file.suffix, "application/octet-stream")
        return Response(file.read_bytes(), content_type=content_type)

    def _send(self, response: Response, include_body: bool = True) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if include_body:
            self.wfile.write(response.body)


class _QuietServer(ThreadingHTTPServer):
    """Ignores clients that hang up early, as tests of size and time limits do."""

    @override
    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


@contextmanager
def run_site() -> Generator[SiteServer]:
    """Start the test site on a free port on 127.0.0.1 and stop it afterwards."""
    server = _QuietServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    site = SiteServer(server)
    handler = type("SiteHandler", (_Handler,), {"site": site})
    server.RequestHandlerClass = handler
    thread = threading.Thread(target=server.serve_forever, name="test-site", daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
