# progdata-crawler

Progdata's service for fetching, crawling, searching and structuring web content for LLMs and agents.

One core, three entry points: the same logic is available through a CLI (`progdata-crawler`, alias `pdc`), an MCP server (`pdc_*` tools) and a REST API (`/v1/...`).

> **Status:** phase 1. Fetching single pages works: clean markdown, cleaned HTML, links, images, metadata and screenshots, from static and JavaScript pages. Crawling, search and LLM extraction come in later phases.

## Principles

- **Polite by default.** robots.txt is respected, requests are rate limited per domain, and the User-Agent is honest: `progdata-crawler/<version> (+https://progdata.no/bot)`. Sites that block crawlers are not worked around.
- **Local first.** Runs for free on your own machine. Results are cached in SQLite.
- **LLM-ready output.** Clean markdown and structured data, not raw HTML.

## Install

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if it is missing.

```bash
git clone https://github.com/progdata-ai/progdata-crawler.git
cd progdata-crawler
uv sync
uv run playwright install chromium   # the browser for JavaScript pages and screenshots
```

Check that it works:

```bash
uv run pdc --version
uv run pdc fetch https://example.com/
```

If you already have Chromium installed, you can skip `playwright install` and set `PDC_BROWSER_EXECUTABLE` to the Chromium binary instead.

## Use it from Claude (MCP)

The MCP server gives Claude the `pdc_fetch` tool. Replace `/path/to/progdata-crawler` with the folder you cloned.

**Claude Code:**

```bash
claude mcp add progdata-crawler -- uv --directory /path/to/progdata-crawler run progdata-crawler mcp
```

**Claude Desktop:** add this to `claude_desktop_config.json` (Settings → Developer → Edit Config) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "progdata-crawler": {
      "command": "uv",
      "args": ["--directory", "/path/to/progdata-crawler", "run", "progdata-crawler", "mcp"]
    }
  }
}
```

On macOS, Claude Desktop may not find `uv` on its `PATH`. Then use the full path from `which uv` as `command`.

Then ask Claude something like "Fetch https://example.com/ and summarize it". Claude calls `pdc_fetch`, which returns the page's main content as markdown plus its metadata.

To serve MCP over HTTP instead of stdio, run `uv run progdata-crawler mcp --http` (default `http://127.0.0.1:8765/mcp`).

## Command line

```bash
pdc fetch https://example.com/article                 # main content as markdown
pdc fetch https://example.com/ --content full         # the whole page
pdc fetch https://example.com/ -o links -o metadata   # other outputs, as JSON
pdc fetch https://example.com/ --json                 # the full response envelope
pdc fetch https://example.com/ --include article --exclude .ads
pdc fetch https://example.com/ -o screenshot --full-page --json
pdc fetch https://example.com/ --no-store             # store nothing after the response
```

Run `pdc fetch --help` for every option. Errors go to stderr with exit code 1.

## REST API

```bash
uv run progdata-crawler serve        # http://127.0.0.1:8000, OpenAPI docs at /docs
```

```bash
curl -s http://127.0.0.1:8000/v1/fetch \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/", "outputs": ["markdown", "links"]}'
```

Every response has the same shape:

```json
{
  "data": { "url": "https://example.com/", "markdown": "# Example Domain\n\n...", "links": [ ... ] },
  "meta": { "request_id": "8f0c...", "took_ms": 412, "cached": false, "rendered": false },
  "error": null
}
```

## Options

The same options work in the CLI, the MCP tool and the REST API.

| Option | Default | Meaning |
| --- | --- | --- |
| `outputs` | `markdown`, `metadata` | Any of `markdown`, `html` (cleaned), `raw_html`, `links`, `images`, `metadata`, `screenshot` (base64 PNG) |
| `content` | `main` | `main` for the main content only, `full` for the whole page |
| `tags` | none | CSS selectors: `include` keeps only those elements, `exclude` removes them |
| `render` | `auto` | When to use the browser: `auto` (when the page needs JavaScript), `always`, `never` |
| `device` | `desktop` | `desktop` (1280×800) or `mobile` (390×844) |
| `locale` | none | Preferred language, sent as `Accept-Language`, e.g. `nb-NO` |
| `wait_ms` | 0 | Extra wait after the page has loaded (uses the browser) |
| `timeout_ms` | 30 000 | Time limit for the page |
| `cache_ttl` | 86 400 | Maximum age of a cached result in seconds; 0 always fetches fresh |
| `full_page_screenshot` | false | Capture the whole page instead of the visible part |
| `retain` | `default` | `none` stores nothing after the response |

## Errors

| Code | HTTP | Meaning |
| --- | --- | --- |
| `invalid_request` | 422 | The request failed validation |
| `robots_disallowed` | 403 | robots.txt does not allow the URL |
| `address_not_allowed` | 403 | The URL points at an internal network address (see `PDC_ALLOW_PRIVATE_NETWORKS`) |
| `blocked_by_site` | 502 | The site refused to serve us (401, 403, 451). We do not retry around it |
| `rate_limited` | 429 | The site asked us to slow down. `Retry-After` says when to try again |
| `fetch_timeout` | 504 | The page did not respond within `timeout_ms` |
| `fetch_failed` | 502 | DNS failure, refused connection, broken TLS, too many redirects |
| `unsupported_type` | 415 | Not an HTML page, or larger than 10 MB |
| `not_found` | 404 | No job with this id, or it has been deleted |
| `internal_error` | 500 | Something went wrong on our side |

## Configuration

Set with environment variables, or in a `.env` file in the working directory.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PDC_DATA_DIR` | `~/.progdata-crawler` | Where the SQLite cache and the job table live |
| `PDC_TIMEOUT_MS` | 30 000 | Default time limit per page |
| `PDC_RATE_LIMIT_INTERVAL_S` | 1.0 | Minimum seconds between requests to the same host |
| `PDC_ALLOW_PRIVATE_NETWORKS` | unset | Whether internal addresses (127.0.0.1, 10.x, 169.254.169.254 …) may be fetched. Unset: allowed for the CLI and MCP over stdio, refused for `serve` and `mcp --http` |
| `PDC_ROBOTS_ALLOWLIST` | `[]` | JSON list of domains where robots.txt is not applied, for our own sites or sites that gave us permission |
| `PDC_BROWSER_EXECUTABLE` | unset | Use this Chromium instead of the one Playwright installs |
| `PDC_BROWSER_MAX_PAGES` | 4 | Pages the browser may render at the same time |
| `PDC_RETENTION_DAYS` | 7 | Cached pages and finished jobs older than this are deleted |
| `PDC_MAX_CONCURRENT_JOBS` | 2 | Jobs (harvest, bulk) that run at the same time |
| `PDC_BULK_CONCURRENCY` | 8 | Pages bulk jobs fetch at the same time, across all bulk jobs |
| `PDC_WEBHOOK_SECRET` | unset | Key that signs webhooks to `notify_url`. Jobs with a `notify_url` are refused while unset |
| `PDC_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `PDC_LOG_FORMAT` | `text` | `text` or `json` (one object per line) |

Logs go to stderr.

## Webhooks

Harvest and bulk jobs accept a `notify_url`. It receives a POST when the job starts and when it ends (`job.started`, `job.succeeded`, `job.failed`, `job.cancelled`, `job.interrupted`). The body is `{"event": ..., "job": {...}}` with the job's status and progress, never page content.

Each request carries `X-PDC-Timestamp` (Unix seconds) and `X-PDC-Signature: sha256=<hex>`, the HMAC-SHA256 of `<timestamp>.<body>` with `PDC_WEBHOOK_SECRET`. Check the signature with a constant-time comparison and reject timestamps more than a few minutes old, so a captured request cannot be replayed:

```python
import hashlib, hmac, time

def valid(secret: bytes, timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, signature)
```

Failed deliveries (network errors, 5xx, 429) are retried after 1, 2, 4, 8 and 16 seconds, within one minute. Webhooks to internal addresses are refused like any other request from a shared entry point, and redirects are not followed.

## Development

```bash
uv sync                      # create .venv and install dependencies
uv run pytest                # run the tests
uv run ruff check            # lint
uv run ruff format           # format
uv run pyright               # type check
uv run pre-commit install    # run the checks on every commit
```

The tests never touch external websites. They run against a local test site (see `tests/support/site_server.py`). See [CLAUDE.md](CLAUDE.md) for the codebase rules and the git workflow.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). You are free to fork, change and use the code, also commercially, as long as you keep the license and the [NOTICE](NOTICE) file. The license does not grant the right to use the Progdata name or trademarks.
