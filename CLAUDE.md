# progdata-crawler

A Python 3.12 service that fetches, crawls, searches and structures web content for LLMs and agents. The same core is exposed through a CLI, an MCP server and a REST API.

## Commands

```bash
uv sync                # install dependencies into .venv
uv run pytest          # tests
uv run ruff check      # lint
uv run ruff format     # format
uv run pyright         # type check (strict)
```

All four must pass before a change is pushed. CI (`.github/workflows/ci.yml`) runs the same checks on every pull request.

CI also checks the licenses of the runtime dependencies. To run that check locally:

```bash
uv sync --no-dev --group licenses
uv run --no-sync pip-licenses --from=mixed --ignore-packages progdata-crawler \
  --partial-match --allow-only="MIT;BSD;Apache;Python Software Foundation;PSF-2.0;Mozilla Public License 2.0;MPL-2.0"
uv sync                # restore the dev environment afterwards
```

Pin third-party GitHub Actions to a full commit SHA, with the version in a comment.

## Rules

- **Project license.** The project is licensed under Apache 2.0 (`LICENSE`, `NOTICE`). Do not add license headers to source files; the root files cover the whole repo.
- **Language.** Everything in the repo is written in English: code, names, comments, docstrings, tests, error messages, docs, commit messages, pull requests and issues.
- **Names.** The package is `progdata-crawler`, the import is `progdata_crawler`, the CLI command is `progdata-crawler` (alias `pdc`), and MCP tools are named `pdc_*`.
- **Original work only.** Do not mention other crawler products by name anywhere: code, comments, docs, tests or commit messages. Do not copy code, test data, documentation or API design from other crawler projects. Write everything from scratch.
- **Licenses.** Python dependencies that are linked into the code must use one of these licenses: MIT, BSD, Apache 2.0, PSF or MPL-2.0. MPL-2.0 packages are used unmodified. No GPL, LGPL or AGPL. Services that run separately in their own container, such as SearXNG, are allowed.
- **Tests.** Never test against external websites. Use the local test site server (`tests/`) instead.
- **Types.** Pyright runs in strict mode. Every interface (CLI, MCP, REST) uses typed Pydantic models from `progdata_crawler.models`.
- **Configuration and logging.** Read settings with `progdata_crawler.settings.get_settings()`, never from `os.environ` directly. Log with `logging.getLogger(__name__)` and pass context as `extra={...}`, not inside the message. Never log secrets or page content.
- **Politeness is not optional.** Respect robots.txt, rate limit per domain and use an honest User-Agent. No proxy rotation, fingerprint spoofing, CAPTCHA solving or other ways around blocking. When a site blocks us, return `blocked_by_site`.

## Git workflow

- One branch and one pull request per issue.
- Pull requests are merged with a merge commit (no squash, no rebase).
- PR titles and commit messages start with the task ID and a colon, for example `P0.1: Set up repository tooling`.
- The PR description closes its issue, for example `Closes #3`.

## Module map

The core never imports from `api/`. The entry points are thin layers on top of it.

```
src/progdata_crawler/
  core/
    fetch/      HTTP and browser fetching, JS detection, browser steps
    polite/     robots.txt and per-domain rate limiting
    extract/    markdown, links, metadata, images, summaries
    discover/   URLs from sitemaps and links
    harvest/    crawling within limits, optionally guided by a goal
    find/       web search providers
    convert/    PDF, DOCX and XLSX to markdown
    structure/  LLM extraction against a schema
    research/   search, fetch and answer with sources
    session/    persistent browser sessions
    watch/      scheduled re-fetching with diffs
    store/      cache, full-text index, job data
    jobs/       queue, workers, notifications
  api/          CLI, MCP server, REST API
  models/       Pydantic models shared by all entry points
  settings.py   configuration from PDC_* environment variables and .env
  log.py        logging setup (text or JSON lines)
```
