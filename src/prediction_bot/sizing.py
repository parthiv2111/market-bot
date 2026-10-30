"""Deterministic paper sizing; capacity estimates are not order authorization."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import Field, model_validator

from prediction_bot.data_quality import _aware
from prediction_bot.reference_data import KOLKATA, FrozenModel
from prediction_bot.session_accounting import SessionState
from prediction_bot.strategy import Proposal

Amount = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=6, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)]


class SizingPolicy(FrozenModel):
    per_trade_risk_pct: Percent
    symbol_pct: Percent
    sector_pct: Percent
    group_pct: Percent
    gross_pct: Percent
    max_positions: int = Field(gt=0, strict=True)
    max_age_seconds: int = Field(gt=0, strict=True)

    @property
    def version(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Costs(FrozenModel):
    buy_per_share: Amount
    sell_per_share: Amount
    buy_minimum: Amount
    sell_minimum: Amount
    slippage_gap_per_share: Amount

    def charges(self, quantity: int) -> tuple[Decimal, Decimal]:
        return (
            max(self.buy_minimum, self.buy_per_share * quantity),
            max(self.sell_minimum, self.sell_per_share * quantity),
        )


class Exposure(FrozenModel):
    identity: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    groups: tuple[str, ...]
    kind: Literal["held", "pending"]
    quantity: int = Field(gt=0, strict=True)
    mark: Amount
    average_entry: Amount
    stop: Amount
    additional_exit_risk: Amount
    reserved_cash: Amount

    @model_validator(mode="after")
    def valid_exposure(self) -> "Exposure":
        if min(self.mark, self.average_entry, self.stop) <= 0:
            raise ValueError("Prices must be positive")
        if (
            not self.groups
            or len(set(self.groups)) != len(self.groups)
            or any(not g for g in self.groups)
        ):
            raise ValueError("Explicit distinct correlation groups are required")
        if self.kind == "held" and self.reserved_cash:
            raise ValueError("Held cash is already spent")
        if self.kind == "pending" and self.reserved_cash < self.mark * self.quantity:
            raise ValueError("Pending buys must reserve at least their full notional")
        return self


class PortfolioCapacity(FrozenModel):
    observed_at: datetime
    reconciled: bool
    cash: Amount
    remaining_risk_budget: Amount
    exposures: tuple[Exposure, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PortfolioCapacity":
        _aware(self.observed_at)
        if len({e.identity for e in self.exposures}) != len(self.exposures):
            raise ValueError("Duplicate exposure identity")
        classifications: dict[str, tuple[str, set[str]]] = {}
        for exposure in self.exposures:
            classification = (exposure.sector, set(exposure.groups))
            if (
                exposure.symbol in classifications
                and classifications[exposure.symbol] != classification
            ):
                raise ValueError("Inconsistent symbol classification")
            classifications[exposure.symbol] = classification
        return self


class EntryTerms(FrozenModel):
    symbol: str = Field(min_length=1)
    sector: str = Field(min_length=1)
    groups: tuple[str, ...]
    entry: Amount
    lot_size: int = Field(gt=0, strict=True)
    liquidity_quantity: int = Field(ge=0, strict=True)
    observed_at: datetime
    costs: Costs

    @model_validator(mode="after")
    def validate_terms(self) -> "EntryTerms":
        _aware(self.observed_at)
        if (
            self.entry <= 0
            or not self.groups
            or len(set(self.groups)) != len(self.groups)
            or any(not g for g in self.groups)
        ):
            raise ValueError("Positive entry and distinct explicit groups required")
        return self


@dataclass(frozen=True)
class SizingDecision:
    action: Literal["SIZED", "NO_TRADE"]
    quantity: int
    planned_risk: Decimal
    purchase_cost: Decimal
    stop: Decimal | None
    policy_version: str
    reasons: tuple[str, ...]


def size_entry(
    proposal: Proposal,
    session: SessionState,
    portfolio: PortfolioCapacity,
    terms: EntryTerms,
    policy: SizingPolicy,
    *,
    now: datetime,
) -> SizingDecision:
    _aware(now)

    def reject(reason: str) -> SizingDecision:
        return SizingDecision(
            "NO_TRADE",
            0,
            Decimal(0),
            Decimal(0),
            proposal.invalidation_stop,
            policy.version,
            (reason,),
        )

    if proposal.action != "BUY" or proposal.symbol != terms.symbol or proposal.mode != "paper":
        return reject("not_matching_paper_buy")
    if session.latch_reasons or not portfolio.reconciled:
        return reject("account_blocked_or_unreconciled")
    for timestamp in (session.observed_at, portfolio.observed_at, terms.observed_at):
        _aware(timestamp)
        if not 0 <= (now - timestamp).total_seconds() <= policy.max_age_seconds:
            return reject("stale_or_future_inputs")
    if session.session_date != now.astimezone(KOLKATA).date():
        return reject("session_mismatch")
    if not session.e0.is_finite() or session.e0 <= 0:
        return reject("invalid_opening_equity")
    if proposal.valid_from is None or proposal.expires_at is None:
        return reject("missing_proposal_window")
    if not proposal.valid_from <= now < proposal.expires_at or proposal.decided_at > now:
        return reject("proposal_outside_window")
    low, high, stop = proposal.entry_low, proposal.entry_high, proposal.invalidation_stop
    if any(value is None or not value.is_finite() for value in (low, high, stop)):
        return reject("invalid_entry_or_stop")
    assert low is not None and high is not None and stop is not None
    if not 0 < stop < low <= terms.entry <= high:
        return reject("invalid_entry_or_stop")
    same = [e for e in portfolio.exposures if e.symbol == terms.symbol]
    for exposure in same:
        if exposure.sector != terms.sector or set(exposure.groups) != set(terms.groups):
            return reject("classification_mismatch")
        if terms.entry < exposure.average_entry or exposure.mark < exposure.average_entry:
            return reject("averaging_down_forbidden")
        if stop < exposure.stop:
            return reject("stop_widening_forbidden")
    symbols = {e.symbol for e in portfolio.exposures}
    if len(symbols | {terms.symbol}) > policy.max_positions:
        return reject("position_count_capacity")
    with localcontext() as context:
        context.prec = 60

        def notional(e: Exposure) -> Decimal:
            return e.mark * e.quantity

        gross = sum((notional(e) for e in portfolio.exposures), Decimal(0))
        symbol = sum((notional(e) for e in same), Decimal(0))
        sector = sum(
            (notional(e) for e in portfolio.exposures if e.sector == terms.sector), Decimal(0)
        )
        capacities = [
            session.e0 * policy.gross_pct / 100 - gross,
            session.e0 * policy.symbol_pct / 100 - symbol,
            session.e0 * policy.sector_pct / 100 - sector,
        ]
        for group in terms.groups:
            used = sum((notional(e) for e in portfolio.exposures if group in e.groups), Decimal(0))
            capacities.append(session.e0 * policy.group_pct / 100 - used)
        cash = portfolio.cash - sum((e.reserved_cash for e in portfolio.exposures), Decimal(0))
        # Supplied account headroom is before heat, after all stricter stop constraints.
        heat = sum(
            (
                max(Decimal(0), e.mark - e.stop) * e.quantity + e.additional_exit_risk
                for e in portfolio.exposures
            ),
            Decimal(0),
        )
        budget = min(
            session.e0 * policy.per_trade_risk_pct / 100, portfolio.remaining_risk_budget - heat
        )
        capacity = min(capacities)
        if min(cash, budget, capacity) <= 0:
            return reject("no_remaining_capacity")

        def estimate(quantity: int) -> tuple[Decimal, Decimal]:
            buy, sell = terms.costs.charges(quantity)
            risk = quantity * (terms.entry - stop + terms.costs.slippage_gap_per_share) + buy + sell
            return risk, quantity * terms.entry + buy

        upper = min(terms.liquidity_quantity, int(capacity // terms.entry)) // terms.lot_size
        left, right = 0, upper
        while left < right:
            middle = (left + right + 1) // 2
            risk, purchase = estimate(middle * terms.lot_size)
            if risk <= budget and purchase <= cash:
                left = middle
            else:
                right = middle - 1
        quantity = left * terms.lot_size
        if quantity == 0:
            return reject("one_lot_exceeds_capacity")
        risk, purchase = estimate(quantity)
        return SizingDecision(
            "SIZED",
            quantity,
            risk,
            purchase,
            stop,
            policy.version,
            ("atomic_reservation_and_entry_gates_required",),
        )
