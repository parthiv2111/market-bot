"""Core schema; prices use decimals and event instants require aware timestamps."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Event timestamps must include a timezone")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        return None if value is None else value.astimezone(UTC)


metadata = MetaData()
instruments = Table(
    "instruments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("exchange", String(16), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
    UniqueConstraint("exchange", "symbol", name="uq_instruments_exchange_symbol"),
)
ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("provider", String(32), nullable=False),
    Column("started_at", UTCDateTime(), nullable=False),
    Column("finished_at", UTCDateTime()),
    Column("status", String(16), nullable=False),
    Column("source_hash", String(64), nullable=False),
    CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_ingestion_status"),
)
candles = Table(
    "candles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("instrument_id", ForeignKey("instruments.id"), nullable=False),
    Column("session_date", Date, nullable=False),
    Column("provider", String(32), nullable=False),
    Column("open", Numeric(20, 6), nullable=False),
    Column("high", Numeric(20, 6), nullable=False),
    Column("low", Numeric(20, 6), nullable=False),
    Column("close", Numeric(20, 6), nullable=False),
    Column("volume", Numeric(24, 0), nullable=False),
    Column("observed_at", UTCDateTime(), nullable=False),
    Column("ingestion_run_id", ForeignKey("ingestion_runs.id"), nullable=False),
    UniqueConstraint("instrument_id", "session_date", "provider", name="uq_candles_identity"),
    CheckConstraint(
        "low > 0 AND high >= low AND open >= low AND open <= high "
        "AND close >= low AND close <= high AND volume >= 0",
        name="ck_candles_ohlcv",
    ),
)
policy_versions = Table(
    "policy_versions",
    metadata,
    Column("version", String(64), primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
)

ledger_accounts = Table("ledger_accounts", metadata, Column("id", String(128), primary_key=True))
ledger_events = Table(
    "ledger_events",
    metadata,
    Column("account_id", ForeignKey("ledger_accounts.id"), primary_key=True),
    Column("event_id", String(128), primary_key=True),
    Column("sequence", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("account_id", "sequence", name="uq_ledger_sequence"),
    CheckConstraint("sequence > 0", name="ck_ledger_sequence"),
)
