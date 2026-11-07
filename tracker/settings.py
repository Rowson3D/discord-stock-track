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
    # On startup, import the old bot's watchlist.json/subscribers.json once (marker file).
    auto_import_legacy: bool = True

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.strip().upper()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'tracker.db').resolve().as_posix()}"


_DEFAULT_MENTION_KINDS = ["back_in_stock", "early_access", "new_listing", "relisted", "deal"]
_PLACEHOLDER_TOKENS = {"replace-me", "YOUR_BOT_TOKEN_HERE"}
_PLACEHOLDER_IDS = {"123456789012345678", ""}


class DiscordSettings(BaseSettings):
    model_config = _config("DISCORD_")

    bot_token: SecretStr | None = None
    # Important alerts (back in stock, Early Access, new, relisted) with pings.
    channel_id: int | None = None
    # Routine radar events (sold out, price, restock ETA, delisted). Defaults to channel_id.
    radar_channel_id: int | None = None
    # Set for instant slash-command sync to one server; global sync can take up to an hour.
    guild_id: int | None = None
    # Extra mention on important alerts: "<@123>", "<@&role_id>", "@here".
    alert_mention: str = ""
    admin_user_ids: CommaList = Field(
        default_factory=list,
        validation_alias=AliasChoices("DISCORD_ADMIN_USER_IDS", "CHECKOUT_ALLOWED_APPROVERS"),
    )
    mention_kinds: CommaList = Field(default_factory=lambda: list(_DEFAULT_MENTION_KINDS))
    # Same alert kind for the same listing within this window is suppressed (flapping).
    alert_cooldown_minutes: float = Field(default=10, ge=0)
    # More routine events than this in one batch are combined into a digest embed.
    digest_threshold: int = Field(default=5, ge=1)

    _split = field_validator("admin_user_ids", "mention_kinds", mode="before")(_split_commas)

    @field_validator("channel_id", "radar_channel_id", "guild_id", mode="before")
    @classmethod
    def _placeholder_id(cls, value: object) -> object:
        # The example env file ships with 123456789012345678; treat it as unset.
        return None if str(value).strip() in _PLACEHOLDER_IDS else value

    @property
    def enabled(self) -> bool:
        if not self.bot_token:
            return False
        token = self.bot_token.get_secret_value().strip()
        return bool(token) and token not in _PLACEHOLDER_TOKENS


class SmsSettings(BaseSettings):
    """Twilio SMS for top-tier alerts. Env names match the original bot."""

    model_config = _config("SMS_")

    enabled: bool = False
    provider: str = "twilio"
    account_sid: str = Field(default="", validation_alias="TWILIO_ACCOUNT_SID")
    auth_token: SecretStr | None = Field(default=None, validation_alias="TWILIO_AUTH_TOKEN")
    from_number: str = Field(default="", validation_alias="TWILIO_FROM_NUMBER")
    to_numbers: CommaList = Field(default_factory=list)
    timeout_seconds: float = 10
    kinds: CommaList = Field(default_factory=lambda: ["back_in_stock", "early_access", "deal"])

    _split = field_validator("to_numbers", "kinds", mode="before")(_split_commas)

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.provider == "twilio"
            and self.account_sid
            and self.auth_token
            and self.from_number
            and self.to_numbers
        )


class CleanupSettings(BaseSettings):
    """Delete the bot's own old messages from alert channels. Env names match the original bot."""

    model_config = _config("MESSAGE_CLEANUP_")

    enabled: bool = False
    ttl_minutes: float = Field(default=60, ge=1)
    interval_minutes: float = Field(default=10, ge=1)
    scan_limit: int = Field(default=200, ge=1)
    max_deletes_per_run: int = Field(default=25, ge=1)
    delete_delay_seconds: float = Field(default=1.25, ge=0.5)


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

    # Non-US stores are served from per-region subdomains (ca/uk/eu.store.ui.com).
    regional_origin_template: str = "https://{region}.store.ui.com"

    @field_validator("base_url", "regional_origin_template")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def origin(self, region: str) -> str:
        if region == "us":
            return self.base_url
        return self.regional_origin_template.format(region=region)


class MonitorSettings(BaseSettings):
    """Polling cadence for the continuous tracker (`tracker run`)."""

    model_config = _config("TRACKER_MONITOR_")

    # Full catalog crawl per region (~10 requests each).
    catalog_interval_seconds: float = Field(default=300, ge=30)
    # Home page check for the freshly-stocked and what's-new feeds (1 request).
    home_interval_seconds: float = Field(default=60, ge=10)
    # How often the hot-listing poller wakes up, and how many product pages it may fetch per run.
    hot_tick_seconds: float = Field(default=10, ge=1)
    hot_products_per_tick: int = Field(default=4, ge=1)
    # Minimum age before a hot listing is polled again.
    hot_poll_interval_seconds: float = Field(default=60, ge=10)
    # What counts as hot besides watched/high-priority/Early Access listings.
    hot_recent_change_hours: float = Field(default=6, ge=0)
    hot_restock_eta_days: int = Field(default=3, ge=0)
    # A listing missing from complete crawls for this long is marked delisted.
    delist_grace_minutes: float = Field(default=45, ge=5)
    # Events older than this when dispatched (e.g. after downtime) are recorded, not announced.
    event_max_age_minutes: float = Field(default=60, ge=1)


class RetailSettings(BaseSettings):
    """Polling for URL-based retailers (Amazon, Best Buy, B&H, Newegg)."""

    model_config = _config("TRACKER_RETAIL_")

    amazon_interval_seconds: float = Field(default=300, ge=30)
    bestbuy_interval_seconds: float = Field(default=120, ge=30)
    bhphoto_interval_seconds: float = Field(default=60, ge=30)
    newegg_interval_seconds: float = Field(default=60, ge=30)
    tick_seconds: float = Field(default=15, ge=1)
    max_per_tick: int = Field(default=3, ge=1)

    def interval_for(self, source: str) -> float:
        return float(getattr(self, f"{source}_interval_seconds", 120))


class EbaySettings(BaseSettings):
    """eBay developer app credentials (https://developer.ebay.com, free)."""

    model_config = _config("EBAY_")

    client_id: str = ""
    client_secret: SecretStr | None = None
    marketplace: str = "EBAY_US"
    api_base: str = "https://api.ebay.com"
    # Sold prices need eBay's Marketplace Insights approval; auto-disabled if refused.
    use_marketplace_insights: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


class MarketSettings(BaseSettings):
    """Resale valuation: fees, costs, comp collection cadence, deal thresholds."""

    model_config = _config("TRACKER_MARKET_")

    # eBay final value fee (most categories) and per-order fee.
    fee_rate: float = Field(default=0.136, ge=0, le=0.5)
    fixed_fee_cents: int = Field(default=40, ge=0)
    # What you pay to ship a sold unit (label + packaging), per unit.
    outbound_shipping_cents: int = Field(default=1200, ge=0)
    purchase_tax_rate: float = Field(default=0.0, ge=0, le=0.3)
    inbound_shipping_cents: int = Field(default=0, ge=0)

    min_comps: int = Field(default=3, ge=1)
    sold_window_days: int = Field(default=90, ge=7)
    # eBay active-listing stats older than this are ignored (sold stats age by sold window).
    active_max_age_hours: float = Field(default=96, ge=1)

    # Comp collection: one variant per tick per source; skip cheap accessories.
    collect_tick_seconds: float = Field(default=60, ge=5)
    collect_per_tick: int = Field(default=1, ge=1)
    refresh_hot_hours: float = Field(default=6, ge=1)
    refresh_hours: float = Field(default=72, ge=1)
    min_retail_cents: int = Field(default=4000, ge=0)
    valuation_interval_seconds: float = Field(default=900, ge=60)

    # A listing that's buyable and clears both thresholds fires a `deal` alert.
    deal_min_profit_cents: int = Field(default=3000, ge=0)
    deal_min_roi: float = Field(default=0.15, ge=0)


class Settings:
    """Aggregate of all settings groups."""

    def __init__(self, env_file: Path | None = ENV_FILE) -> None:
        """`env_file=None` reads only real environment variables (used by tests)."""
        self.app = AppSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.discord = DiscordSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.http = HttpSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.unifi = UnifiSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.monitor = MonitorSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.retail = RetailSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.sms = SmsSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.cleanup = CleanupSettings(_env_file=env_file)  # type: ignore[call-arg]
        self.ebay = EbaySettings(_env_file=env_file)  # type: ignore[call-arg]
        self.market = MarketSettings(_env_file=env_file)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
