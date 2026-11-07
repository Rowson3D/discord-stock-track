"""Database schema.

products  ─< variants ─< listings ─< stock_observations
                                  ─< events
                                  ─< watches   (listing_id NULL = subscribed to everything)
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    DDL,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tracker.db.types import UTCDateTime
from tracker.domain import Priority, StockStatus

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    brand: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(100))
    msrp_cents: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    variants: Mapped[list[Variant]] = relationship(back_populates="product")


class Variant(Base):
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    sku: Mapped[str] = mapped_column(String(100))
    # Lower-cased SKU used for lookups; SKUs are case-insensitive in practice.
    sku_key: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str | None] = mapped_column(String(200))
    pack_size: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Last time resale comps were collected for this variant.
    comps_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    product: Mapped[Product] = relationship(back_populates="variants")
    listings: Mapped[list[Listing]] = relationship(back_populates="variant")


class Listing(Base):
    """A variant as sold by one retailer in one region. This is what gets polled."""

    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "region", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000))
    priority: Mapped[str] = mapped_column(String(16), default=Priority.NORMAL.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Latest known state (denormalized from the newest observation for fast reads).
    status: Mapped[str] = mapped_column(String(32), default=StockStatus.UNKNOWN.value)
    raw_status: Mapped[str | None] = mapped_column(String(64))
    price_cents: Mapped[int | None] = mapped_column(Integer)
    regular_price_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(8))
    is_early_access: Mapped[bool] = mapped_column(Boolean, default=False)
    restock_eta: Mapped[date | None] = mapped_column(Date)
    sold_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_in_stock_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Set when the listing vanished from the store; cleared when it's seen again.
    delisted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_unknowns: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    variant: Mapped[Variant] = relationship(back_populates="listings")


class StockObservation(Base):
    """Append-only history; one row per detected change in a listing's state."""

    __tablename__ = "stock_observations"
    __table_args__ = (Index("ix_stock_observations_listing_time", "listing_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(32))
    price_cents: Mapped[int | None] = mapped_column(Integer)
    regular_price_cents: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[int | None] = mapped_column(Integer)
    is_early_access: Mapped[bool] = mapped_column(Boolean, default=False)
    restock_eta: Mapped[date | None] = mapped_column(Date)

    listing: Mapped[Listing] = relationship()


class Event(Base):
    """Something alert-worthy happened: the launch-radar feed and alert queue."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    listing: Mapped[Listing | None] = relationship()


class MarketComp(Base):
    """A manual resale data point (e.g. your own sale) for a variant.

    eBay listings are never stored here: they're summarized in memory into `EbayStats`
    (the eBay app declares it does not persist eBay data).
    """

    __tablename__ = "market_comps"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(32))  # ebay_sold | ebay_active | manual
    external_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(1000))
    price_cents: Mapped[int] = mapped_column(Integer)  # Whole listing (all units).
    shipping_cents: Mapped[int | None] = mapped_column(Integer)  # Paid by the buyer.
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    condition: Mapped[str] = mapped_column(String(16), default="unknown")
    quantity: Mapped[int] = mapped_column(Integer, default=1)  # Units in the listing (lots).
    sold: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    match_score: Mapped[float] = mapped_column(Float, default=1.0)
    excluded_reason: Mapped[str | None] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(32))


class EbayStats(Base):
    """Aggregate eBay prices per variant from the latest fetch. No listing-level data."""

    __tablename__ = "ebay_stats"

    variant_id: Mapped[int] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), primary_key=True
    )
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    sold_count: Mapped[int] = mapped_column(Integer, default=0)
    sold_30d: Mapped[int] = mapped_column(Integer, default=0)
    sold_median_cents: Mapped[int | None] = mapped_column(Integer)
    sold_p25_cents: Mapped[int | None] = mapped_column(Integer)
    sold_p75_cents: Mapped[int | None] = mapped_column(Integer)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    active_median_cents: Mapped[int | None] = mapped_column(Integer)
    active_p25_cents: Mapped[int | None] = mapped_column(Integer)
    active_low_cents: Mapped[int | None] = mapped_column(Integer)
    # Matching listings before collapsing by seller (active_count = distinct sellers).
    active_listings: Mapped[int] = mapped_column(Integer, default=0)
    # How many fetched results matched this variant vs. were rejected (counts only).
    matched: Mapped[int] = mapped_column(Integer, default=0)
    excluded: Mapped[int] = mapped_column(Integer, default=0)


class EbayStatsHistory(Base):
    """One row per variant per UTC day: the day's latest eBay aggregates (no listing data).

    eBay listings are never stored, so this history can't be backfilled. It exists only
    from the day collection starts, which is why it records every collection.
    """

    __tablename__ = "ebay_stats_history"
    __table_args__ = (UniqueConstraint("variant_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    retail_cents: Mapped[int | None] = mapped_column(Integer)
    sold_count: Mapped[int] = mapped_column(Integer, default=0)
    sold_30d: Mapped[int] = mapped_column(Integer, default=0)
    sold_median_cents: Mapped[int | None] = mapped_column(Integer)
    sold_p25_cents: Mapped[int | None] = mapped_column(Integer)
    sold_p75_cents: Mapped[int | None] = mapped_column(Integer)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    active_listings: Mapped[int] = mapped_column(Integer, default=0)
    active_median_cents: Mapped[int | None] = mapped_column(Integer)
    active_p25_cents: Mapped[int | None] = mapped_column(Integer)
    active_low_cents: Mapped[int | None] = mapped_column(Integer)


class Valuation(Base):
    """Latest resale valuation per variant (recomputed periodically)."""

    __tablename__ = "valuations"

    variant_id: Mapped[int] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), primary_key=True
    )
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    retail_cents: Mapped[int | None] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    basis: Mapped[str | None] = mapped_column(String(16))  # sold | active | manual
    resale_cents: Mapped[int | None] = mapped_column(Integer)
    fees_cents: Mapped[int | None] = mapped_column(Integer)
    shipping_cents: Mapped[int | None] = mapped_column(Integer)
    cost_cents: Mapped[int | None] = mapped_column(Integer)
    net_cents: Mapped[int | None] = mapped_column(Integer)
    roi: Mapped[float | None] = mapped_column(Float)
    sold_count: Mapped[int] = mapped_column(Integer, default=0)
    sold_30d: Mapped[int] = mapped_column(Integer, default=0)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    manual_count: Mapped[int] = mapped_column(Integer, default=0)
    sold_median_cents: Mapped[int | None] = mapped_column(Integer)
    sold_p25_cents: Mapped[int | None] = mapped_column(Integer)
    sold_p75_cents: Mapped[int | None] = mapped_column(Integer)
    active_median_cents: Mapped[int | None] = mapped_column(Integer)
    active_low_cents: Mapped[int | None] = mapped_column(Integer)
    sell_through: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str] = mapped_column(String(8), default="none")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    # True while the deal alert condition holds, so `deal` fires once per crossing.
    deal_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # Scarcity pricing: eBay on days the store was sold out (see valuation.py).
    scarcity_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    scarcity_resale_cents: Mapped[int | None] = mapped_column(Integer)
    scarcity_net_cents: Mapped[int | None] = mapped_column(Integer)
    scarcity_roi: Mapped[float | None] = mapped_column(Float)
    # The credible profit deals and rankings use: sold | manual | scarcity.
    flip_basis: Mapped[str | None] = mapped_column(String(16))
    flip_net_cents: Mapped[int | None] = mapped_column(Integer)
    flip_roi: Mapped[float | None] = mapped_column(Float)
    flip_confidence: Mapped[str] = mapped_column(String(8), default="none", server_default="none")


class Meta(Base):
    """Small key/value store for durable process state (e.g. completed baselines)."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Guild(Base):
    """A Discord server the bot may serve, with its own alert configuration.

    Only `approved` servers get alerts or accept commands; the bot leaves the rest.
    """

    __tablename__ = "guilds"

    guild_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|blocked
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_by: Mapped[str | None] = mapped_column(String(32))
    alert_channel_id: Mapped[str | None] = mapped_column(String(32))
    radar_channel_id: Mapped[str | None] = mapped_column(String(32))
    alert_mention: Mapped[str | None] = mapped_column(String(100))
    admin_role_id: Mapped[str | None] = mapped_column(String(32))
    mention_kinds: Mapped[str | None] = mapped_column(String(300))  # CSV; NULL = defaults
    cooldown_minutes: Mapped[float | None] = mapped_column(Float)
    digest_threshold: Mapped[int | None] = mapped_column(Integer)
    # eBay-derived numbers (resale, profit, deals). Off by default: showing them to other
    # people is "displaying eBay content" under eBay's license.
    show_valuations: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """Append-only record of administrative actions (enforced by database triggers)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32))  # NULL = the system itself
    actor_name: Mapped[str | None] = mapped_column(String(100))
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text)  # JSON


class Watch(Base):
    """A Discord user wants alerts for a listing (or for everything when listing_id is NULL)."""

    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("discord_user_id", "listing_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), index=True)
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    # Only ping when the listing's price is at or below this.
    max_price_cents: Mapped[int | None] = mapped_column(Integer)
    # The server the watch was set in; pings go there. NULL (pre-Phase 4) = the home server.
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# The audit log is append-only at the database level, not just by convention.
AUDIT_APPEND_ONLY_DDL = (
    "CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
)
for _statement in AUDIT_APPEND_ONLY_DDL:
    event.listen(AuditLog.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))
