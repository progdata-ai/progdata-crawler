# progdata-crawler

Progdata's service for fetching, crawling, searching and structuring web content for LLMs and agents.

One core, three entry points: the same logic is available through a CLI (`progdata-crawler`, alias `pdc`), an MCP server (`pdc_*` tools) and a REST API (`/v1/...`).

> Status: early development. Nothing is usable yet. See the [phase 0](https://github.com/progdata-ai/progdata-crawler/issues/1) and [phase 1](https://github.com/progdata-ai/progdata-crawler/issues/2) tracking issues.

## Principles

- **Polite by default.** robots.txt is respected, requests are rate limited per domain, and the User-Agent is honest.
- **Local first.** Runs for free on your own machine and can scale to the cloud later.
- **LLM-ready output.** Clean markdown and structured data, not raw HTML.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if it is missing.

```bash
uv sync                      # create .venv and install dependencies
uv run pytest                # run the tests
uv run ruff check            # lint
uv run ruff format           # format
uv run pyright               # type check
uv run pre-commit install    # run the checks on every commit
```

See [CLAUDE.md](CLAUDE.md) for the codebase rules and the git workflow.
