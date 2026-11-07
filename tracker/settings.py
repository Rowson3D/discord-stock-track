"""Typed, validated configuration loaded from environment variables and `.env`.

Each settings group owns an env prefix. Legacy variable names from the original bot
(`STOCK_BOT_*`) are still accepted where a direct equivalent exists.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

CommaList = Annotated[list[str], NoDecode]


def _config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )


def _split_commas(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class AppSettings(BaseSettings):
    model_config = _config("TRACKER_")

    data_dir: Path = Field(
        default=PROJECT_ROOT / "data",
        validation_alias=AliasChoices("TRACKER_DATA_DIR", "STOCK_BOT_DATA_DIR"),
    )
    database_url: str | None = None
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("TRACKER_LOG_LEVEL", "STOCK_BOT_LOG_LEVEL"),
    )
    log_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("TRACKER_LOG_FILE", "STOCK_BOT_LOG_FILE"),
    )
    log_json: bool = False

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.strip().upper()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'tracker.db').resolve().as_posix()}"


class DiscordSettings(BaseSettings):
    model_config = _config("DISCORD_")

    bot_token: SecretStr | None = None
    channel_id: int | None = None
    alert_mention: str = ""


class HttpSettings(BaseSettings):
    model_config = _config("TRACKER_HTTP_")

    timeout_seconds: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    per_host_concurrency: int = Field(default=2, ge=1)
    per_host_min_interval_seconds: float = Field(default=1.0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)


class UnifiSettings(BaseSettings):
    model_config = _config("UNIFI_STORE_")

    base_url: str = "https://store.ui.com"
    regions: CommaList = Field(default_factory=lambda: ["us"])
    language: str = "en"
    # Category slugs to crawl in addition to those discovered from the store home page.
    extra_categories: CommaList = Field(default_factory=list)

    _split = field_validator("regions", "extra_categories", mode="before")(_split_commas)

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")


class Settings:
    """Aggregate of all settings groups."""

    def __init__(self, env_file: Path | None = ENV_FILE) -> None:
        """`env_file=None` reads only real environment variables (used by tests)."""
        self.app = AppSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.discord = DiscordSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.http = HttpSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.unifi = UnifiSettings(_env_file=env_file)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
