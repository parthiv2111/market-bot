"""Real PostgreSQL tests use a disposable schema inside a dedicated test database."""

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError

from prediction_bot import cli
from prediction_bot.data_quality import QualityStore
from prediction_bot.ingestion import persist_batch
from prediction_bot.persistence.database import database_engine, database_healthy
from prediction_bot.persistence.schema import (
    candles,
    ingestion_runs,
    instruments,
    metadata,
    policy_versions,
)
from prediction_bot.providers import FetchRequest

pytestmark = pytest.mark.postgres


def test_ledger_persistence_idempotency_and_rollback(database):
    from prediction_bot.ledger import LedgerEvent, ingest_event, read_ledger
    from prediction_bot.persistence.schema import ledger_events

    connection, config = database
    command.upgrade(config, "head")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    deposit = LedgerEvent(
        event_id="deposit", occurred_at=now, kind="deposit", source="manual", amount="1000"
    )
    fill = LedgerEvent(
        event_id="fill",
        occurred_at=now,
        kind="buy",
        source="manual",
        instrument_id="A",
        quantity=3,
        price="100",
        fee="1",
    )
    ingest_event(connection, "account", deposit)
    expected = ingest_event(connection, "account", fill)
    connection.commit()
    assert ingest_event(connection, "account", fill) == expected
    connection.commit()
    assert read_ledger(connection, "account") == expected
    assert len(connection.execute(select(ledger_events)).all()) == 2
    connection.commit()
    with pytest.raises(ValueError, match="Conflicting"), connection.begin():
        ingest_event(connection, "account", fill.model_copy(update={"price": Decimal(101)}))
    with pytest.raises(ValueError, match="inventory"), connection.begin():
        ingest_event(
            connection,
            "account",
            fill.model_copy(update={"event_id": "oversell", "kind": "sell", "quantity": 4}),
        )
    assert read_ledger(connection, "account") == expected
    connection.commit()
    # A valid event is not durable until its caller commits.
    ingest_event(connection, "account", fill.model_copy(update={"event_id": "rolled-back"}))
    connection.rollback()
    assert read_ledger(connection, "account") == expected
    connection.commit()
    for statement in (ledger_events.update().values(sequence=10), ledger_events.delete()):
        with pytest.raises(DBAPIError), connection.begin():
            connection.execute(statement)
    assert ingest_event(connection, "other-account", deposit).cash == 1000
    connection.commit()
    assert read_ledger(connection, "account") == expected


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
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
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


def test_ingestion_replay_and_conflict_rollback(database, tmp_path):
    connection, config = database
    command.upgrade(config, "head")
    payload = (
        b"exchange,symbol,session_date,open,high,low,close,volume\n"
        b"NSE,SYNTH,2026-01-02,100,110,90,105,1000\n"
    )
    batch = QualityStore(tmp_path).import_csv(
        payload,
        FetchRequest(("SYNTH",), date(2026, 1, 2), date(2026, 1, 2), (date(2026, 1, 2),)),
        known_at=datetime(2026, 1, 3, tzinfo=UTC),
        usage_basis="synthetic",
    )
    connection.commit()
    persist_batch(connection, batch, "job-1", "fixture")
    # Simulates a crash after commit but before the filesystem checkpoint.
    persist_batch(connection, batch, "job-1", "fixture")
    persist_batch(connection, batch, "job-2", "fixture")
    assert len(connection.execute(select(candles)).all()) == 1
    assert connection.scalar(select(candles.c.ingestion_run_id)) == "job-1"
    assert connection.scalar(select(candles.c.observed_at)) == batch.known_at
    connection.commit()
    conflicting = replace(batch, bars=(replace(batch.bars[0], close=Decimal(106)),))
    with pytest.raises(ValueError, match="Conflicting candle"):
        persist_batch(connection, conflicting, "job-3", "fixture")
    assert (
        connection.scalar(select(ingestion_runs.c.id).where(ingestion_runs.c.id == "job-3")) is None
    )
    assert connection.scalar(select(candles.c.close)) == 105
    connection.commit()
    with pytest.raises(ValueError, match="Run identity"):
        persist_batch(connection, replace(batch, source_sha256="f" * 64), "job-1", "fixture")
    precise = replace(batch, bars=(replace(batch.bars[0], close=Decimal("105.0000001")),))
    with pytest.raises(ValueError, match="precision"):
        persist_batch(connection, precise, "job-4", "fixture")


def test_ingestion_cli_writes_and_releases_lock(database, tmp_path, monkeypatch, capsys):
    connection, config = database
    command.upgrade(config, "head")
    connection.commit()

    class ScopedEngine:
        @contextmanager
        def connect(self):
            yield connection

        def dispose(self):
            pass

    monkeypatch.setattr(cli, "database_engine", lambda _: ScopedEngine())
    monkeypatch.setenv("PREDICTION_BOT_DATABASE_URL", "fixture-only")
    source = tmp_path / "source.csv"
    source.write_text(
        "exchange,symbol,session_date,open,high,low,close,volume\n"
        "NSE,SYNTH,2026-01-02,100,110,90,105,1000\n"
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "revision": "fixture",
                "mode": "daily",
                "start": "2026-01-02",
                "end": "2026-01-02",
                "sources": {"SYNTH": str(source)},
                "usage_basis": "synthetic",
            }
        )
    )
    reference = tmp_path / "reference.json"
    reference.write_text(
        json.dumps(
            {
                "source": "synthetic",
                "published_at": "2026-01-01T00:00:00Z",
                "coverage_start": "2026-01-02",
                "coverage_end": "2026-01-02",
                "aliases": [
                    {
                        "instrument_id": "stable",
                        "exchange_id": "id",
                        "symbol": "SYNTH",
                        "valid_from": "2026-01-01",
                        "valid_to": "2027-01-01",
                    }
                ],
                "days": [
                    {
                        "session_date": "2026-01-02",
                        "status": "open",
                        "reason": "synthetic",
                        "opens_at": "2026-01-02T09:15:00+05:30",
                        "closes_at": "2026-01-02T15:30:00+05:30",
                    }
                ],
            }
        )
    )
    args = [
        "ingest",
        str(plan),
        "--reference",
        str(reference),
        "--directory",
        str(tmp_path / "store"),
    ]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["symbols"]["SYNTH"]["status"] == "completed"
    assert cli.main(args) == 0
    assert len(connection.execute(select(candles)).all()) == 1
    assert (
        connection.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
        )
        == 0
    )
