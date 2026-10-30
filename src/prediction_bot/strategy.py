"""Pure research/paper proposals; no broker, transport or execution capability."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from prediction_bot.data_quality import _aware
from prediction_bot.reference_data import KOLKATA, FrozenModel, ReferenceBundle
from prediction_bot.regimes import RegimeDecision


class StrategyPolicy(FrozenModel):
    implementation: Literal["long-only-proposals-v1"] = "long-only-proposals-v1"
    buy_probability: float = Field(gt=0, lt=1, allow_inf_nan=False)
    sell_probability: float = Field(gt=0, lt=1, allow_inf_nan=False)
    entry_band_bps: Decimal = Field(gt=0, lt=10000, allow_inf_nan=False)
    stop_atr_multiple: Decimal = Field(gt=0, allow_inf_nan=False)
    max_signal_age_seconds: int = Field(gt=0, strict=True)
    max_inventory_age_seconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def validate_thresholds(self) -> "StrategyPolicy":
        if self.sell_probability >= self.buy_probability:
            raise ValueError("SELL threshold must be below BUY threshold")
        return self

    @property
    def version(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class ModelSignal(FrozenModel):
    symbol: str = Field(min_length=1)
    signal_at: datetime
    generated_at: datetime
    model_version: str = Field(min_length=1)
    label_version: Literal["daily-five-session-v1"] = "daily-five-session-v1"
    probability_positive_net_return: float = Field(ge=0, le=1, allow_inf_nan=False)
    reference_close: Decimal = Field(gt=0, allow_inf_nan=False)
    atr: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_timing(self) -> "ModelSignal":
        _aware(self.signal_at)
        _aware(self.generated_at)
        if self.generated_at < self.signal_at:
            raise ValueError("Signal generation cannot precede its information cutoff")
        return self


class InventorySnapshot(FrozenModel):
    symbol: str = Field(min_length=1)
    owned_quantity: int = Field(ge=0, strict=True)
    reserved_sell_quantity: int = Field(default=0, ge=0, strict=True)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_inventory(self) -> "InventorySnapshot":
        _aware(self.observed_at)
        if self.reserved_sell_quantity > self.owned_quantity:
            raise ValueError("Reserved sells cannot exceed owned inventory")
        return self


@dataclass(frozen=True)
class Proposal:
    action: Literal["BUY", "SELL", "NO_TRADE"]
    symbol: str
    signal_at: datetime
    decided_at: datetime
    model_version: str
    regime_version: str
    policy_version: str
    valid_from: datetime | None
    expires_at: datetime | None
    entry_low: Decimal | None
    entry_high: Decimal | None
    invalidation_stop: Decimal | None
    holding_sessions: int | None
    planned_exit_at: datetime | None
    quantity: int | None
    reduce_only: bool
    reasons: tuple[str, ...]
    mode: Literal["paper"] = "paper"


def propose(
    signal: ModelSignal,
    regime: RegimeDecision | None,
    inventory: InventorySnapshot,
    reference: ReferenceBundle,
    policy: StrategyPolicy,
    *,
    now: datetime,
) -> Proposal:
    _aware(now)

    def decision(
        action: Literal["BUY", "SELL", "NO_TRADE"],
        reasons: tuple[str, ...],
        *,
        valid_from: datetime | None = None,
        expires_at: datetime | None = None,
        low: Decimal | None = None,
        high: Decimal | None = None,
        stop: Decimal | None = None,
        exit_at: datetime | None = None,
        quantity: int | None = None,
    ) -> Proposal:
        return Proposal(
            action,
            signal.symbol,
            signal.signal_at,
            now,
            signal.model_version,
            regime.rule_version if regime else "unavailable",
            policy.version,
            valid_from,
            expires_at,
            low,
            high,
            stop,
            5 if action == "BUY" else None,
            exit_at,
            quantity,
            action == "SELL",
            reasons,
        )

    failures = []
    if signal.generated_at > now or signal.signal_at > now:
        failures.append("future_signal")
    if (now - signal.signal_at).total_seconds() > policy.max_signal_age_seconds:
        failures.append("stale_signal")
    if inventory.symbol != signal.symbol:
        failures.append("inventory_symbol_mismatch")
    if (
        inventory.observed_at > now
        or (now - inventory.observed_at).total_seconds() > policy.max_inventory_age_seconds
    ):
        failures.append("stale_or_future_inventory")
    day = signal.signal_at.astimezone(KOLKATA).date()
    try:
        session = reference.day(day)
        if session.closes_at != signal.signal_at:
            failures.append("not_completed_session_signal")
        reference.resolve(signal.symbol, day)
        next_days = reference.sessions_after(day, 5)
        entry_session = reference.day(next_days[0])
        exit_session = reference.day(next_days[-1])
    except LookupError:
        return decision("NO_TRADE", tuple(failures) + ("reference_coverage_unavailable",))
    if failures:
        return decision("NO_TRADE", tuple(failures))
    assert entry_session.opens_at is not None and entry_session.closes_at is not None
    if now >= entry_session.closes_at:
        return decision("NO_TRADE", ("proposal_expired",))
    probability = signal.probability_positive_net_return
    if probability <= policy.sell_probability:
        available = inventory.owned_quantity - inventory.reserved_sell_quantity
        if available == 0:
            return decision("NO_TRADE", ("no_available_owned_inventory",))
        return decision(
            "SELL",
            ("low_five_session_probability", "reduce_owned_inventory_only"),
            valid_from=entry_session.opens_at,
            expires_at=entry_session.closes_at,
            quantity=available,
        )
    if probability < policy.buy_probability:
        return decision("NO_TRADE", ("probability_in_neutral_band",))
    if regime is None or regime.regime == "unknown":
        return decision("NO_TRADE", ("unknown_regime",))
    if (
        regime.session_date != day
        or regime.as_of != signal.signal_at
        or regime.inputs.get("equity.symbol") != signal.symbol
    ):
        return decision("NO_TRADE", ("regime_signal_mismatch",))
    if not regime.entry_eligible:
        return decision("NO_TRADE", ("regime_blocks_entries",))
    band = policy.entry_band_bps / Decimal(10000)
    low = signal.reference_close * (1 - band)
    high = signal.reference_close * (1 + band)
    stop = signal.reference_close - policy.stop_atr_multiple * signal.atr
    if not 0 < stop < low:
        return decision("NO_TRADE", ("invalid_invalidation_stop",))
    return decision(
        "BUY",
        ("probability_and_regime_eligible", "risk_approval_required"),
        valid_from=entry_session.opens_at,
        expires_at=entry_session.closes_at,
        low=low,
        high=high,
        stop=stop,
        exit_at=exit_session.closes_at,
    )
