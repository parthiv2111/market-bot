"""Checkpointed, offline-first historical/daily ingestion of permitted CSV sources."""

import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from prediction_bot.data_quality import QualityStore, ValidatedBatch
from prediction_bot.persistence.schema import candles, ingestion_runs, instruments
from prediction_bot.providers import FetchRequest
from prediction_bot.reference_data import FrozenModel, ReferenceBundle


class IngestionPlan(FrozenModel):
    revision: str = Field(min_length=1)
    mode: Literal["historical", "daily"]
    start: date
    end: date
    sources: dict[str, str]
    usage_basis: str = Field(min_length=1)
    provider: str = Field(default="local_csv", min_length=1, max_length=32)
    max_attempts: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def validate_plan(self) -> "IngestionPlan":
        if self.end < self.start or (self.mode == "daily" and self.start != self.end):
            raise ValueError("Invalid historical/daily date range")
        if not self.sources or any(
            not symbol.strip() or not path for symbol, path in self.sources.items()
        ):
            raise ValueError("Require nonempty symbols and source paths")
        return self


def job_identity(plan: IngestionPlan, reference: ReferenceBundle) -> str:
    payload = {"plan": plan.model_dump(mode="json"), "reference": reference.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(json.dumps(payload, sort_keys=True).encode())
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid checkpoint")
    return value


def run_ingestion(
    plan: IngestionPlan,
    reference: ReferenceBundle,
    directory: Path,
    sink: Callable[[ValidatedBatch, str], None],
    *,
    read_source: Callable[[Path], bytes] = Path.read_bytes,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Caller serializes a job (CLI uses a PostgreSQL session advisory lock)."""
    identity = job_identity(plan, reference)
    root = directory / "jobs" / identity
    sessions = []
    day = plan.start
    while day <= plan.end:
        if reference.day(day).status == "open":
            sessions.append(day)
        day += timedelta(days=1)
    _checkpoint(
        root / "plan.json",
        {"plan": plan.model_dump(mode="json"), "reference": reference.model_dump(mode="json")},
    )
    quality = QualityStore(directory / "quality")
    states: dict[str, object] = {}
    for symbol, source in sorted(plan.sources.items()):
        name = hashlib.sha256(symbol.encode()).hexdigest()
        checkpoint = root / f"{name}.json"
        state = _read(checkpoint) if checkpoint.exists() else {"symbol": symbol, "attempts": 0}
        try:
            identities = {reference.resolve(symbol, day).instrument_id for day in sessions}
            if len(identities) > 1:
                raise ValueError("Split the job at instrument identity changes")
        except (LookupError, ValueError):
            state.update(status="failed", error="instrument_identity")
            _checkpoint(checkpoint, state)
            states[symbol] = state
            continue
        if state.get("status") == "completed":
            states[symbol] = state
            continue
        if not sessions:
            state.update(status="skipped", reason="no_open_sessions")
            _checkpoint(checkpoint, state)
            states[symbol] = state
            continue
        for attempt in range(plan.max_attempts):
            state["attempts"] = int(str(state["attempts"])) + 1
            state.update(status="running")
            _checkpoint(checkpoint, state)
            try:
                if "source_sha256" in state:
                    source_hash = str(state["source_sha256"])
                    if len(source_hash) != 64 or any(
                        c not in "0123456789abcdef" for c in source_hash
                    ):
                        raise ValueError("Invalid cached source hash")
                    payload = (directory / "quality" / "raw" / f"{source_hash}.csv").read_bytes()
                    if hashlib.sha256(payload).hexdigest() != source_hash:
                        raise ValueError("Cached source integrity failure")
                    known_at = datetime.fromisoformat(str(state["known_at"]))
                else:
                    payload = read_source(Path(source))
                    known_at = now()
                query = FetchRequest((symbol,), plan.start, plan.end, tuple(sessions))
                batch = quality.import_csv(
                    payload, query, known_at=known_at, usage_basis=plan.usage_basis
                )
                state.update(
                    source_sha256=batch.source_sha256,
                    known_at=known_at.isoformat(),
                    validation_version=batch.report_version,
                )
                _checkpoint(checkpoint, state)  # Pin bytes before any database mutation.
                if batch.quarantine or batch.missing_sessions:
                    state.update(
                        status="failed",
                        error="data_quality",
                        quarantined=len(batch.quarantine),
                        missing=len(batch.missing_sessions),
                    )
                    break
                run_id = str(uuid5(NAMESPACE_URL, identity + ":" + symbol))
                sink(batch, run_id)
                state.update(status="completed", run_id=run_id, rows=len(batch.bars))
                state.pop("error", None)
                break
            except OSError as error:
                state.update(status="failed", error=type(error).__name__)
                _checkpoint(checkpoint, state)
                if attempt + 1 < plan.max_attempts:
                    sleep(float(2**attempt))
            except Exception as error:
                # Persist a safe diagnostic and continue other symbols. Interrupts propagate.
                state.update(status="failed", error=type(error).__name__)
                break
        _checkpoint(checkpoint, state)
        states[symbol] = state
    report: dict[str, object] = {
        "job_id": identity,
        "symbols": states,
        "status": "failed"
        if any(isinstance(s, dict) and s.get("status") == "failed" for s in states.values())
        else "completed",
    }
    _checkpoint(root / "report.json", report)
    return report


def persist_batch(
    connection: Connection, batch: ValidatedBatch, run_id: str, provider: str
) -> None:
    """One symbol is atomic; identical observations are no-ops, conflicts fail closed."""
    if batch.quarantine or batch.missing_sessions:
        raise ValueError("Only complete validated batches may be persisted")
    with connection.begin():
        connection.execute(
            insert(ingestion_runs)
            .values(
                id=run_id,
                provider=provider,
                started_at=batch.known_at,
                status="running",
                source_hash=batch.source_sha256,
            )
            .on_conflict_do_nothing(index_elements=[ingestion_runs.c.id])
        )
        existing_run = (
            connection.execute(select(ingestion_runs).where(ingestion_runs.c.id == run_id))
            .mappings()
            .one()
        )
        if (
            existing_run["provider"] != provider
            or existing_run["source_hash"] != batch.source_sha256
        ):
            raise ValueError("Run identity conflicts with stored source")
        if existing_run["status"] == "completed":
            return
        for bar in batch.bars:
            # Do not silently round source prices into the fixed database scale.
            if any(
                value != value.quantize(Decimal("0.000001"))
                for value in (bar.open, bar.high, bar.low, bar.close)
            ):
                raise ValueError("Source price precision exceeds database scale")
            connection.execute(
                insert(instruments)
                .values(exchange=bar.exchange, symbol=bar.symbol, created_at=batch.known_at)
                .on_conflict_do_nothing(
                    index_elements=[instruments.c.exchange, instruments.c.symbol]
                )
            )
            instrument_id = connection.scalar(
                select(instruments.c.id).where(
                    instruments.c.exchange == bar.exchange, instruments.c.symbol == bar.symbol
                )
            )
            values = dict(
                instrument_id=instrument_id,
                session_date=bar.session_date,
                provider=provider,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                observed_at=batch.known_at,
                ingestion_run_id=run_id,
            )
            connection.execute(
                insert(candles)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        candles.c.instrument_id,
                        candles.c.session_date,
                        candles.c.provider,
                    ]
                )
            )
            stored = (
                connection.execute(
                    select(candles).where(
                        candles.c.instrument_id == instrument_id,
                        candles.c.session_date == bar.session_date,
                        candles.c.provider == provider,
                    )
                )
                .mappings()
                .one()
            )
            if any(
                stored[column] != values[column]
                for column in ("open", "high", "low", "close", "volume")
            ):
                raise ValueError("Conflicting candle requires explicit reconciliation")
        connection.execute(
            ingestion_runs.update()
            .where(ingestion_runs.c.id == run_id)
            .values(status="completed", finished_at=datetime.now(UTC))
        )
