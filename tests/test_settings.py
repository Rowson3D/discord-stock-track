from __future__ import annotations

from pathlib import Path

import pytest

from tracker.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "TRACKER_DATA_DIR",
        "STOCK_BOT_DATA_DIR",
        "TRACKER_DATABASE_URL",
        "TRACKER_LOG_LEVEL",
        "STOCK_BOT_LOG_LEVEL",
        "STOCK_BOT_LOG_FILE",
        "UNIFI_STORE_REGIONS",
        "DISCORD_CHANNEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    settings = Settings(env_file=None)
    assert settings.unifi.regions == ["us"]
    assert settings.unifi.base_url == "https://store.ui.com"
    assert settings.app.resolved_database_url.startswith("sqlite+aiosqlite:///")
    assert settings.app.resolved_database_url.endswith("/tracker.db")
    assert settings.discord.bot_token is None


def test_legacy_env_names_still_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("STOCK_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_BOT_LOG_LEVEL", "debug")
    monkeypatch.setenv("STOCK_BOT_LOG_FILE", "")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123456789012345678")

    settings = Settings(env_file=None)
    assert settings.app.data_dir == tmp_path
    assert settings.app.log_level == "DEBUG"
    assert settings.app.log_file is None
    assert settings.discord.channel_id == 123456789012345678
    assert settings.app.resolved_database_url.endswith(f"{tmp_path.as_posix()}/tracker.db")


def test_comma_separated_regions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UNIFI_STORE_REGIONS", "us, ca ,uk")
    assert Settings(env_file=None).unifi.regions == ["us", "ca", "uk"]


def test_explicit_database_url_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRACKER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    assert Settings(env_file=None).app.resolved_database_url == "sqlite+aiosqlite:///:memory:"
