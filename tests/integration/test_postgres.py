"""Real PostgreSQL tests use a disposable schema inside a dedicated test database."""

import os
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError

from prediction_bot.persistence.database import database_engine, database_healthy
from prediction_bot.persistence.schema import (
    candles,
    ingestion_runs,
    instruments,
    metadata,
    policy_versions,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def database():
    url = os.environ.get("PREDICTION_BOT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PREDICTION_BOT_TEST_DATABASE_URL for real PostgreSQL tests")
    engine = database_engine(url, testing=True)
    assert database_healthy(engine)
    schema = "test_" + uuid4().hex
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.commit()
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["connection"] = connection
        try:
            yield connection, config
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            connection.commit()
    engine.dispose()


def test_empty_migration_and_data_preserving_upgrade(database):
    connection, config = database
    assert inspect(connection).get_table_names() == []
    command.upgrade(config, "0001")
    connection.execute(
        instruments.insert().values(
            exchange="NSE",
            symbol="SYNTHETIC",
            created_at=datetime.now(UTC),
        )
    )
    connection.commit()
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # Re-running migration is idempotent.
    assert connection.scalar(select(instruments.c.symbol)) == "SYNTHETIC"
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
    assert compare_metadata(MigrationContext.configure(connection), metadata) == []


def test_candle_identity_foreign_keys_and_utc_roundtrip(database):
    connection, config = database
    command.upgrade(config, "head")
    local = datetime(2026, 1, 1, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    identifier = connection.scalar(
        instruments.insert()
        .values(
            exchange="NSE",
            symbol="SYNTHETIC",
            created_at=local,
        )
        .returning(instruments.c.id)
    )
    connection.execute(
        ingestion_runs.insert().values(
            id="run-1",
            provider="fixture",
            started_at=local,
            status="running",
            source_hash="a" * 64,
        )
    )
    row = dict(
        instrument_id=identifier,
        session_date=date(2026, 1, 1),
        provider="fixture",
        open=100,
        high=110,
        low=90,
        close=105,
        volume=1000,
        observed_at=local,
        ingestion_run_id="run-1",
    )
    connection.execute(candles.insert().values(**row))
    connection.commit()
    timestamp = connection.scalar(select(candles.c.observed_at))
    assert timestamp == datetime(2026, 1, 1, 3, 45, tzinfo=UTC)
    assert timestamp.utcoffset() == timedelta(0)
    assert connection.scalar(text("SHOW timezone")) == "UTC"
    for mutation in [{}, {"instrument_id": -1}, {"provider": "invalid-ohlcv", "low": -1}]:
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(candles.insert().values(**{**row, **mutation}))
    with pytest.raises(StatementError), connection.begin_nested():
        connection.execute(candles.insert().values(**{**row, "observed_at": datetime(2026, 1, 1)}))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            instruments.insert().values(
                exchange="NSE",
                symbol="SYNTHETIC",
                created_at=local,
            )
        )
    connection.execute(candles.insert().values(**{**row, "provider": "different-provider"}))


def test_policy_history_cannot_be_mutated(database):
    connection, config = database
    command.upgrade(config, "head")
    connection.execute(
        policy_versions.insert().values(
            version="a" * 64,
            payload={"mode": "paper"},
            created_at=datetime.now(UTC),
        )
    )
    connection.commit()
    for statement in [policy_versions.update().values(payload={}), policy_versions.delete()]:
        with pytest.raises(DBAPIError, match="immutable"), connection.begin_nested():
            connection.execute(statement)
    assert connection.scalar(select(policy_versions.c.payload)) == {"mode": "paper"}
