"""Auditable local candle validation and explicitly dated research adjustments."""

import csv
import hashlib
import io
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import Field, model_validator

from prediction_bot.providers import FetchRequest, PriceBar, ProviderError, parse_csv
from prediction_bot.reference_data import KOLKATA, FrozenModel


def _aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Knowledge timestamps must be timezone-aware")


def _save(directory: Path, payload: bytes, suffix: str) -> str:
    version = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{version}.{suffix}"
    with NamedTemporaryFile(dir=directory, delete=False) as temporary:
        path = Path(temporary.name)
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        try:
            os.link(path, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise ValueError("Stored artifact failed integrity verification") from None
    finally:
        path.unlink(missing_ok=True)
    return version


class CorporateAction(FrozenModel):
    action_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    kind: Literal["split", "cash_dividend"]
    effective_at: datetime
    known_at: datetime
    source: str = Field(min_length=1)
    # Source-supplied multipliers, never inferred from a price discontinuity.
    price_factor: Decimal = Field(gt=0, allow_inf_nan=False)
    new_shares_per_old: Decimal = Field(default=Decimal(1), gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_action(self) -> "CorporateAction":
        _aware(self.effective_at)
        _aware(self.known_at)
        if self.kind == "split":
            if self.price_factor != Decimal(1) / self.new_shares_per_old:
                raise ValueError("Split price factor must be the reciprocal share ratio")
        elif self.new_shares_per_old != 1 or self.price_factor > 1:
            raise ValueError(
                "Cash dividends require a price factor at most one and no share change"
            )
        return self


@dataclass(frozen=True)
class QuarantinedRow:
    line: int
    cells: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ValidatedBatch:
    source_sha256: str
    known_at: datetime
    bars: tuple[PriceBar, ...]
    quarantine: tuple[QuarantinedRow, ...]
    missing_sessions: tuple[tuple[str, str], ...]
    report_version: str


class QualityStore:
    """Content-addressed raw bytes, validation reports, actions and research series."""

    def __init__(self, directory: Path):
        self.directory = directory

    def import_csv(
        self, payload: bytes, request: FetchRequest, *, known_at: datetime, usage_basis: str
    ) -> ValidatedBatch:
        _aware(known_at)
        if not usage_basis.strip() or request.expected_sessions is None:
            raise ValueError("Require a permitted-use basis and explicit expected sessions")
        source = _save(self.directory / "raw", payload, "csv")
        rejected: list[QuarantinedRow] = []
        candidates: list[tuple[int, tuple[str, ...], PriceBar]] = []
        try:
            reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
            header = next(reader, [])

            def encode(cells: list[str] | tuple[str, ...] | None = None) -> bytes:
                stream = io.StringIO(newline="")
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(header)
                if cells is not None:
                    writer.writerow(cells)
                return stream.getvalue().encode()

            parse_csv(encode())
            for row in reader:
                cells = tuple(row)
                try:
                    bar = parse_csv(encode(cells))[0]
                    if (
                        bar.symbol not in request.symbols
                        or not request.start <= bar.session_date <= request.end
                    ):
                        raise ProviderError("Outside requested symbol/date range")
                    if bar.session_date not in request.expected_sessions:
                        raise ProviderError("Unexpected trading session")
                    if bar.session_date >= known_at.astimezone(KOLKATA).date():
                        raise ProviderError("Require completed prior-day candles")
                    candidates.append((reader.line_num, cells, bar))
                except ProviderError as error:
                    rejected.append(QuarantinedRow(reader.line_num, cells, str(error)))
        except (UnicodeDecodeError, csv.Error, ProviderError):
            # A broken envelope makes row boundaries/schema unreliable. Keep the full source.
            candidates.clear()
            rejected.append(QuarantinedRow(0, (), "Invalid file encoding, quoting or header"))
        counts = Counter((bar.exchange, bar.symbol, bar.session_date) for _, _, bar in candidates)
        accepted = []
        for line, cells, bar in candidates:
            if counts[(bar.exchange, bar.symbol, bar.session_date)] > 1:
                rejected.append(QuarantinedRow(line, cells, "Duplicate candle identity"))
            else:
                accepted.append(bar)
        bars = tuple(sorted(accepted, key=lambda bar: (bar.symbol, bar.session_date)))
        observed = {(bar.symbol, bar.session_date) for bar in bars}
        missing = tuple(
            (symbol, day.isoformat())
            for symbol in sorted(request.symbols)
            for day in sorted(request.expected_sessions)
            if (symbol, day) not in observed
        )
        report = {
            "schema_version": 1,
            "source_sha256": source,
            "known_at": known_at.isoformat(),
            "usage_basis": usage_basis,
            "request": asdict(request),
            "raw_bars": [asdict(bar) for bar in bars],
            "quarantine": [asdict(row) for row in rejected],
            "missing_sessions": missing,
        }
        version = _save(
            self.directory / "validation",
            json.dumps(report, sort_keys=True, default=str).encode(),
            "json",
        )
        return ValidatedBatch(source, known_at, bars, tuple(rejected), missing, version)

    def research_series(
        self, batch: ValidatedBatch, actions: tuple[CorporateAction, ...], *, as_of: datetime
    ) -> tuple[PriceBar, ...]:
        _aware(as_of)
        if batch.known_at > as_of:
            raise ValueError("Raw observations were not known at this cutoff")
        if len({action.action_id for action in actions}) != len(actions):
            raise ValueError("Duplicate action IDs require explicit revision resolution")
        action_payload = json.dumps(
            [action.model_dump(mode="json") for action in actions], sort_keys=True
        ).encode()
        action_version = _save(self.directory / "actions", action_payload, "json")
        output = []
        for bar in batch.bars:
            factor = Decimal(1)
            for action in actions:
                if (
                    action.symbol == bar.symbol
                    and action.known_at <= as_of
                    and action.effective_at <= as_of
                    and bar.session_date < action.effective_at.astimezone(KOLKATA).date()
                ):
                    factor *= action.price_factor
            # Ignore vendor-adjusted columns: their knowledge time is not established.
            output.append(
                replace(
                    bar,
                    adjusted_open=bar.open * factor,
                    adjusted_high=bar.high * factor,
                    adjusted_low=bar.low * factor,
                    adjusted_close=bar.close * factor,
                )
            )
        series = tuple(output)
        payload = {
            "schema_version": 1,
            "validation_version": batch.report_version,
            "action_version": action_version,
            "as_of": as_of.isoformat(),
            "bars": [asdict(bar) for bar in series],
        }
        _save(
            self.directory / "research",
            json.dumps(payload, sort_keys=True, default=str).encode(),
            "json",
        )
        return series
