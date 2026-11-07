"""Database schema.

products  ─< variants ─< listings ─< stock_observations
                                  ─< events
                                  ─< watches   (listing_id NULL = subscribed to everything)
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
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


class Meta(Base):
    """Small key/value store for durable process state (e.g. completed baselines)."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
