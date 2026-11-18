import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from progdata_crawler import __version__
from progdata_crawler.settings import LogFormat, LogLevel, Settings, default_user_agent


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run every test in an empty directory without PDC_ variables from the outside."""
    monkeypatch.chdir(tmp_path)
    for name in [name for name in os.environ if name.startswith("PDC_")]:
        monkeypatch.delenv(name)


def test_defaults() -> None:
    settings = Settings()
    assert settings.user_agent == f"progdata-crawler/{__version__} (+https://progdata.no/bot)"
    assert settings.timeout_ms == 30_000
    assert settings.data_dir == Path.home() / ".progdata-crawler"
    assert settings.cache_db_path == settings.data_dir / "cache.sqlite3"
    assert settings.log_level is LogLevel.INFO
    assert settings.log_format is LogFormat.TEXT
    assert settings.rate_limit_interval_s == 1.0


def test_user_agent_names_product_version_and_info_page() -> None:
    agent = default_user_agent()
    assert agent.startswith("progdata-crawler/")
    assert __version__ in agent
    assert "https://progdata.no/bot" in agent


def test_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PDC_TIMEOUT_MS", "5000")
    monkeypatch.setenv("PDC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PDC_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PDC_LOG_FORMAT", "json")
    settings = Settings()
    assert settings.timeout_ms == 5000
    assert settings.cache_db_path == tmp_path / "data" / "cache.sqlite3"
    assert settings.log_level is LogLevel.DEBUG
    assert settings.log_format is LogFormat.JSON


def test_reads_dotenv_file_and_environment_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("PDC_TIMEOUT_MS=7000\nPDC_LOG_LEVEL=WARNING\n")
    monkeypatch.setenv("PDC_LOG_LEVEL", "ERROR")
    settings = Settings()
    assert settings.timeout_ms == 7000
    assert settings.log_level is LogLevel.ERROR


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PDC_TIMEOUT_MS", "0"),
        ("PDC_TIMEOUT_MS", "999999"),
        ("PDC_LOG_LEVEL", "LOUD"),
        ("PDC_LOG_FORMAT", "xml"),
        ("PDC_USER_AGENT", ""),
        ("PDC_RATE_LIMIT_INTERVAL_S", "0"),
        ("PDC_RATE_LIMIT_INTERVAL_S", "61"),
    ],
)
def test_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_robots_allowlist_is_read_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().robots_allowlist == ()
    monkeypatch.setenv("PDC_ROBOTS_ALLOWLIST", '["progdata.no", "example.org"]')
    assert Settings().robots_allowlist == ("progdata.no", "example.org")
