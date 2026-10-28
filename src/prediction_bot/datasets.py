"""Long-only daily five-session research labels and separated feature snapshots."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from prediction_bot.data_quality import CorporateAction, _aware, _save
from prediction_bot.indicators import NAMES, IndicatorConfig, KnownBar, features_at
from prediction_bot.providers import export_csv
from prediction_bot.reference_data import KOLKATA, FrozenModel, ReferenceBundle
from prediction_bot.universe import Universe


class LabelPolicy(FrozenModel):
    implementation: Literal["daily-five-session-v1"] = "daily-five-session-v1"
    fee_bps_per_side: Decimal = Field(ge=0, lt=10000, allow_inf_nan=False)
    slippage_bps_per_side: Decimal = Field(ge=0, lt=10000, allow_inf_nan=False)
    action_coverage_start: date
    action_coverage_end: date
    action_source: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_coverage(self) -> "LabelPolicy":
        if self.action_coverage_end < self.action_coverage_start:
            raise ValueError("Invalid corporate-action coverage")
        return self


@dataclass(frozen=True)
class Label:
    entry_at: datetime
    exit_at: datetime
    entry_open: str
    exit_close: str
    net_return: float
    positive: int


@dataclass(frozen=True)
class DatasetRow:
    instrument_id: str
    symbol: str
    signal_at: datetime
    feature_values: tuple[float, ...]
    target: Label


@dataclass(frozen=True)
class ExcludedRow:
    session_date: date
    symbol: str | None
    reason: str


@dataclass(frozen=True)
class DatasetSnapshot:
    feature_schema: tuple[str, ...]
    label_schema: tuple[str, ...]
    rows: tuple[DatasetRow, ...]
    excluded: tuple[ExcludedRow, ...]
    provenance: dict[str, str]

    def feature_matrix(self) -> tuple[tuple[float, ...], ...]:
        # No target dictionary or user-controlled target column selection is involved.
        if any(name not in NAMES for name in self.feature_schema):
            raise ValueError("Unknown or target column in feature schema")
        if any(len(row.feature_values) != len(self.feature_schema) for row in self.rows):
            raise ValueError("Feature schema mismatch")
        return tuple(row.feature_values for row in self.rows)

    def save(self, directory: Path) -> str:
        return _save(
            directory,
            json.dumps(asdict(self), sort_keys=True, default=str, allow_nan=False).encode(),
            "json",
        )


def build_dataset(
    observations: tuple[KnownBar, ...],
    universe: Universe,
    reference: ReferenceBundle,
    actions: tuple[CorporateAction, ...],
    *,
    start: date,
    end: date,
    as_of: datetime,
    policy: LabelPolicy,
    feature_schema: tuple[str, ...] = ("return", "sma", "ema", "rsi", "atr", "volatility"),
    indicators: IndicatorConfig | None = None,
) -> DatasetSnapshot:
    _aware(as_of)
    if end < start:
        raise ValueError("Invalid dataset range")
    if (
        not feature_schema
        or len(set(feature_schema)) != len(feature_schema)
        or any(name not in NAMES for name in feature_schema)
    ):
        raise ValueError("Require distinct indicator feature columns; target columns are forbidden")
    indicators = indicators or IndicatorConfig()
    # Index only observations available by dataset creation; future labels never feed features.
    available: dict[tuple[str, date], KnownBar] = {}
    for observation in observations:
        _aware(observation.known_at)
        if observation.known_at > as_of:
            continue
        bar = observation.bar
        session = reference.day(bar.session_date)
        if session.closes_at is None or observation.known_at < session.closes_at:
            raise ValueError(
                "Require completed daily observations with honest knowledge timestamps"
            )
        if session.closes_at > as_of:
            continue
        export_csv((bar,))
        instrument = reference.resolve(bar.symbol, bar.session_date).instrument_id
        key = (instrument, bar.session_date)
        if key in available:
            raise ValueError("Duplicate instrument/session observation")
        available[key] = observation
    rows = []
    excluded = []
    day = start
    while day <= end:
        try:
            session = reference.day(day)
        except LookupError:
            excluded.append(ExcludedRow(day, None, "calendar_unavailable"))
            day += timedelta(days=1)
            continue
        if session.closes_at is None:
            day += timedelta(days=1)
            continue
        if session.closes_at > as_of:
            excluded.append(ExcludedRow(day, None, "incomplete_signal_session"))
            day += timedelta(days=1)
            continue
        try:
            members = universe.members(day, as_of=session.closes_at)
        except LookupError:
            excluded.append(ExcludedRow(day, None, "universe_unavailable"))
            day += timedelta(days=1)
            continue
        for member in members:
            if any(
                item.instrument_id == member.instrument_id and item.known_at <= as_of
                for item in universe.limitations
            ):
                excluded.append(ExcludedRow(day, member.symbol, "declared_history_limitation"))
                continue
            try:
                identity = reference.resolve(member.symbol, day)
                if identity.instrument_id != member.instrument_id:
                    raise LookupError
                future = reference.sessions_after(day, 5)
            except LookupError:
                excluded.append(ExcludedRow(day, member.symbol, "identity_or_horizon_unavailable"))
                continue
            window = [(member.instrument_id, d) for d in future]
            if any(key not in available for key in window):
                excluded.append(ExcludedRow(day, member.symbol, "incomplete_five_session_horizon"))
                continue
            if any(available[key].bar.volume == 0 for key in window):
                excluded.append(ExcludedRow(day, member.symbol, "nontrading_horizon_observation"))
                continue
            entry = available[window[0]].bar
            exit_bar = available[window[-1]].bar
            entry_at = reference.day(entry.session_date).opens_at
            exit_at = reference.day(exit_bar.session_date).closes_at
            assert entry_at is not None and exit_at is not None
            if entry_at <= session.closes_at or exit_at > as_of:
                excluded.append(ExcludedRow(day, member.symbol, "nonexecutable_timing"))
                continue
            if (
                not policy.action_coverage_start
                <= entry.session_date
                <= exit_bar.session_date
                <= policy.action_coverage_end
            ):
                excluded.append(
                    ExcludedRow(day, member.symbol, "corporate_action_coverage_unavailable")
                )
                continue
            has_action = False
            for action in actions:
                if action.known_at > as_of:
                    continue
                if entry_at < action.effective_at <= exit_at:
                    try:
                        action_identity = reference.resolve(
                            action.symbol, action.effective_at.astimezone(KOLKATA).date()
                        )
                    except LookupError:
                        has_action = True  # An unresolved event makes this horizon uncertain.
                        break
                    if action_identity.instrument_id == member.instrument_id:
                        has_action = True
                        break
            if has_action:
                excluded.append(
                    ExcludedRow(day, member.symbol, "corporate_action_in_holding_window")
                )
                continue
            vector = features_at(
                observations,
                reference,
                symbol=member.symbol,
                session_date=day,
                as_of=session.closes_at,
                config=indicators,
            )
            features = [vector.values[name] for name in feature_schema]
            if any(
                feature.value is None or feature.invalid_reason is not None for feature in features
            ):
                excluded.append(ExcludedRow(day, member.symbol, "invalid_features"))
                continue
            fee = policy.fee_bps_per_side / Decimal(10000)
            slip = policy.slippage_bps_per_side / Decimal(10000)
            paid = entry.open * (1 + slip) * (1 + fee)
            received = exit_bar.close * (1 - slip) * (1 - fee)
            net = float(received / paid - 1)
            if not math.isfinite(net):
                excluded.append(ExcludedRow(day, member.symbol, "numeric_overflow"))
                continue
            rows.append(
                DatasetRow(
                    member.instrument_id,
                    member.symbol,
                    session.closes_at,
                    tuple(
                        float(feature.value) for feature in features if feature.value is not None
                    ),
                    Label(
                        entry_at, exit_at, str(entry.open), str(exit_bar.close), net, int(net > 0)
                    ),
                )
            )
        day += timedelta(days=1)
    provenance = {
        "policy": policy.model_dump_json(),
        "indicators": indicators.model_dump_json(),
        "universe_sha256": hashlib.sha256(universe.model_dump_json().encode()).hexdigest(),
        "reference_sha256": hashlib.sha256(reference.model_dump_json().encode()).hexdigest(),
        "observations_sha256": hashlib.sha256(
            json.dumps(
                [asdict(item) for item in observations], sort_keys=True, default=str
            ).encode()
        ).hexdigest(),
        "actions_sha256": hashlib.sha256(
            json.dumps([item.model_dump(mode="json") for item in actions], sort_keys=True).encode()
        ).hexdigest(),
        "as_of": as_of.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feature_implementation": "indicators-v1",
    }
    return DatasetSnapshot(
        feature_schema, ("net_return", "positive"), tuple(rows), tuple(excluded), provenance
    )
