"""Service configuration.

Every setting can be set with an environment variable prefixed with `PDC_`, for example
`PDC_LOG_LEVEL=DEBUG`, or in a `.env` file in the working directory. Environment
variables win over the `.env` file.
"""

from enum import StrEnum
from functools import cache
from pathlib import Path

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from progdata_crawler import __version__
from progdata_crawler.models.options import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS

BOT_INFO_URL = "https://progdata.no/bot"


def default_user_agent() -> str:
    """The honest User-Agent: product, version and a page that explains the bot."""
    return f"progdata-crawler/{__version__} (+{BOT_INFO_URL})"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LogFormat(StrEnum):
    TEXT = "text"
    """Readable lines for local use."""
    JSON = "json"
    """One JSON object per line, for log collectors."""


class SearchProviderName(StrEnum):
    BRAVE = "brave"
    """The Brave Search API. Needs `PDC_BRAVE_API_KEY`."""
    LOCAL = "local"
    """Pages already fetched, through the full-text index. Free, offline, no key."""


class LlmProviderName(StrEnum):
    ANTHROPIC = "anthropic"
    """Claude through the Anthropic API. Needs `PDC_ANTHROPIC_API_KEY`."""
    AZURE_OPENAI = "azure_openai"
    """Azure OpenAI. Needs the endpoint, key and API version; the model is the deployment."""
    OLLAMA = "ollama"
    """A local Ollama server. Needs `PDC_LLM_MODEL`."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    user_agent: str = Field(default_factory=default_user_agent, min_length=1)
    """Sent with every request. Must identify us honestly."""
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, gt=0, le=MAX_TIMEOUT_MS)
    """Default time limit per page when a request does not set one."""
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".progdata-crawler")
    """Directory for local data such as the SQLite cache."""
    rate_limit_interval_s: float = Field(default=1.0, ge=0.1, le=60)
    """Minimum seconds between requests to the same host. robots.txt Crawl-delay can
    make it longer, never shorter."""
    allow_private_networks: bool | None = None
    """Whether fetching internal addresses (127.0.0.1, 10.x, 169.254.169.254 ...) is allowed.
    Unset: allowed for the CLI and MCP over stdio, which run on your own machine, and
    refused for `serve` and `mcp --http`, which others can reach."""
    browser_executable: Path | None = None
    """Chromium to use instead of the one Playwright installs. Leave unset normally."""
    browser_max_pages: int = Field(default=4, ge=1, le=32)
    """Pages the browser may render at the same time."""
    retention_days: int = Field(default=7, ge=1, le=365)
    """Cached pages and finished jobs older than this are deleted automatically."""
    max_concurrent_jobs: int = Field(default=2, ge=1, le=32)
    """Jobs (harvest, bulk) that run at the same time."""
    bulk_concurrency: int = Field(default=8, ge=1, le=64)
    """Pages that bulk jobs fetch at the same time, across all bulk jobs. The per-domain
    rate limit still spaces out requests to the same site."""
    webhook_secret: SecretStr | None = None
    """Key for signing webhooks to `notify_url`. Jobs with a `notify_url` are refused
    while it is unset."""
    search_provider: SearchProviderName = SearchProviderName.BRAVE
    """Which search provider find uses."""
    brave_api_key: SecretStr | None = None
    """Key for the Brave Search API."""
    brave_api_url: str = "https://api.search.brave.com/res/v1"
    """Address of the Brave Search API. Change it only for tests."""
    session_ttl_s: int = Field(default=300, ge=10, le=3_600)
    """A browser session closes after this many seconds without use."""
    session_max_age_s: int = Field(default=1_800, ge=60, le=24 * 3_600)
    """A browser session closes after this many seconds, used or not."""
    max_sessions: int = Field(default=4, ge=1, le=64)
    """Browser sessions open at the same time."""
    llm_provider: LlmProviderName | None = None
    """The LLM for summaries, structured extraction and goals. Unset: those features are off."""
    llm_model: str | None = None
    """Model name (Azure OpenAI: the deployment name). Anthropic defaults to claude-sonnet-5-5."""
    anthropic_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    """For example https://my-resource.openai.azure.com."""
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str | None = None
    ollama_url: str = "http://localhost:11434/v1"
    llm_timeout_s: float = Field(default=60.0, gt=0, le=600)
    """Time limit for one LLM call, retries included."""
    llm_max_input_chars: int = Field(default=100_000, ge=1_000, le=2_000_000)
    """Longer input is cut to this length before it is sent."""
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.TEXT

    @computed_field
    @property
    def jobs_db_path(self) -> Path:
        """The SQLite database for jobs and their results."""
        return self.data_dir / "jobs.sqlite3"

    @computed_field
    @property
    def cache_db_path(self) -> Path:
        """The SQLite database for the page cache and full-text index."""
        return self.data_dir / "cache.sqlite3"


@cache
def get_settings() -> Settings:
    """The settings for this process, read once from the environment."""
    return Settings()
