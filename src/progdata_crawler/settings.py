"""Service configuration.

Every setting can be set with an environment variable prefixed with `PDC_`, for example
`PDC_LOG_LEVEL=DEBUG`, or in a `.env` file in the working directory. Environment
variables win over the `.env` file.
"""

from enum import StrEnum
from functools import cache
from pathlib import Path

from pydantic import Field, computed_field
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
    robots_allowlist: tuple[str, ...] = ()
    """Domains where robots.txt is not applied: our own sites and sites that have given
    us permission. Subdomains are included. Set as JSON, e.g. `["progdata.no"]`."""
    browser_executable: Path | None = None
    """Chromium to use instead of the one Playwright installs. Leave unset normally."""
    browser_max_pages: int = Field(default=4, ge=1, le=32)
    """Pages the browser may render at the same time."""
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.TEXT

    @computed_field
    @property
    def cache_db_path(self) -> Path:
        """The SQLite database for the page cache and full-text index."""
        return self.data_dir / "cache.sqlite3"


@cache
def get_settings() -> Settings:
    """The settings for this process, read once from the environment."""
    return Settings()
