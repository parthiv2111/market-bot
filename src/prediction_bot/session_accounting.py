"""Durable exchange-session baselines and cash-flow-neutral risk accounting."""

import hashlib
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from prediction_bot.data_quality import _aware
from prediction_bot.ledger import Money
from prediction_bot.persistence.schema import ledger_accounts, session_snapshots
from prediction_bot.reference_data import KOLKATA, FrozenModel, ReferenceBundle


class EquityObservation(FrozenModel):
    observation_id: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    equity: Money
    cumulative_external_flows: Decimal = Field(allow_inf_nan=False, max_digits=24, decimal_places=6)
    reconciled: bool
    fresh_marks: bool
    session_open: bool = False
    new_entries: int = Field(default=0, ge=0, strict=True)
    campaign_result: Literal["none", "win", "loss", "flat"] = "none"
    latch_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_time(self) -> "EquityObservation":
        _aware(self.observed_at)
        if any(not reason for reason in self.latch_reasons):
            raise ValueError("Latch reasons cannot be empty")
        return self


class SessionState(FrozenModel):
    observed_at: datetime
    session_date: date
    week: tuple[int, int]
    month: tuple[int, int]
    e0: Decimal
    week_e0: Decimal
    month_e0: Decimal
    day_flow_base: Decimal
    week_flow_base: Decimal
    month_flow_base: Decimal
    cumulative_external_flows: Decimal
    equity: Decimal
    daily_pnl: Decimal
    weekly_pnl: Decimal
    monthly_pnl: Decimal
    session_high: Decimal
    unit_index: Decimal
    lifetime_peak: Decimal
    drawdown: Decimal
    session_entries: int
    completed_campaigns: int
    consecutive_losses: int
    latch_reasons: tuple[str, ...]
    reference_version: str


def advance_session(
    previous: SessionState | None, observation: EquityObservation, reference: ReferenceBundle
) -> SessionState:
    """Require separate valuations around cash flows; never infer a midnight reset."""
    if not observation.reconciled or not observation.fresh_marks:
        raise ValueError("Reconciled equity and fresh marks are required")
    day = reference.day(observation.observed_at.astimezone(KOLKATA).date())
    if day.status != "open":
        raise ValueError("Not an exchange session")
    assert day.opens_at is not None and day.closes_at is not None
    if not day.opens_at <= observation.observed_at <= day.closes_at:
        raise ValueError("Observation is outside exchange-session boundaries")
    opening = previous is None or previous.session_date != day.session_date
    if opening != observation.session_open or (opening and observation.observed_at != day.opens_at):
        raise ValueError("A new session requires an explicit reconciled opening valuation")
    if previous and observation.observed_at < previous.observed_at:
        raise ValueError("Observations must be chronological")
    if (
        opening
        and previous
        and reference.sessions_after(previous.session_date, 1)[0] != day.session_date
    ):
        raise ValueError("Missing intervening session opening; reconcile before continuing")
    iso = day.session_date.isocalendar()
    week = (iso.year, iso.week)
    month = (day.session_date.year, day.session_date.month)
    equity = observation.equity
    flows = observation.cumulative_external_flows
    version = hashlib.sha256(reference.model_dump_json().encode()).hexdigest()
    with localcontext() as context:
        context.prec = 60
        if previous is None:
            if equity <= 0:
                raise ValueError("Initial equity must be positive")
            index = peak = Decimal(1)
            week_e0 = month_e0 = equity
            week_flow = month_flow = flows
            losses = 0
            latches: tuple[str, ...] = ()
        else:
            delta = flows - previous.cumulative_external_flows
            if delta:
                if equity - previous.equity != delta:
                    raise ValueError("Cash flows require separate before/after valuations")
                if previous.equity <= 0 or equity <= 0:
                    raise ValueError("Cannot unitize funding at zero equity; reconcile explicitly")
                index = previous.unit_index
            else:
                if previous.equity <= 0:
                    raise ValueError("Cannot resume from zero equity without reconciliation")
                index = previous.unit_index * equity / previous.equity
            peak = max(previous.lifetime_peak, index)
            week_e0 = equity if week != previous.week else previous.week_e0
            month_e0 = equity if month != previous.month else previous.month_e0
            week_flow = flows if week != previous.week else previous.week_flow_base
            month_flow = flows if month != previous.month else previous.month_flow_base
            losses = previous.consecutive_losses
            latches = previous.latch_reasons
        if opening:
            e0, day_flow = equity, flows
            entries = completed = 0
            high = Decimal(0)
        else:
            assert previous is not None
            e0, day_flow = previous.e0, previous.day_flow_base
            entries, completed = previous.session_entries, previous.completed_campaigns
            high = previous.session_high
        daily = equity - e0 - (flows - day_flow)
        if observation.campaign_result == "loss":
            losses += 1
        elif observation.campaign_result in ("win", "flat"):
            losses = 0
        reasons = set(latches) | set(observation.latch_reasons)
        if equity == 0:
            reasons.add("zero_equity")
        if previous and flows != previous.cumulative_external_flows:
            reasons.add("funding_reconciliation_required")
        return SessionState(
            observed_at=observation.observed_at,
            session_date=day.session_date,
            week=week,
            month=month,
            e0=e0,
            week_e0=week_e0,
            month_e0=month_e0,
            day_flow_base=day_flow,
            week_flow_base=week_flow,
            month_flow_base=month_flow,
            cumulative_external_flows=flows,
            equity=equity,
            daily_pnl=daily,
            weekly_pnl=equity - week_e0 - (flows - week_flow),
            monthly_pnl=equity - month_e0 - (flows - month_flow),
            session_high=max(high, daily),
            unit_index=index,
            lifetime_peak=peak,
            drawdown=(peak - index) / peak,
            session_entries=entries + observation.new_entries,
            completed_campaigns=completed + int(observation.campaign_result != "none"),
            consecutive_losses=losses,
            latch_reasons=tuple(sorted(reasons)),
            reference_version=version,
        )


def read_session(connection: Connection, account_id: str) -> SessionState | None:
    row = connection.execute(
        select(session_snapshots.c.state)
        .where(session_snapshots.c.account_id == account_id)
        .order_by(session_snapshots.c.sequence.desc())
        .limit(1)
    ).first()
    return None if row is None else SessionState.model_validate(row[0])


def persist_observation(
    connection: Connection,
    account_id: str,
    observation: EquityObservation,
    reference: ReferenceBundle,
) -> SessionState:
    """Caller owns transaction; observations and projections commit together."""
    if not account_id or len(account_id) > 128:
        raise ValueError("Account identity must contain 1 to 128 characters")
    connection.execute(insert(ledger_accounts).values(id=account_id).on_conflict_do_nothing())
    connection.execute(
        select(ledger_accounts.c.id).where(ledger_accounts.c.id == account_id).with_for_update()
    ).one()
    previous = read_session(connection, account_id)
    fingerprint = hashlib.sha256(
        (observation.model_dump_json() + reference.model_dump_json()).encode()
    ).hexdigest()
    existing = connection.execute(
        select(session_snapshots.c.fingerprint).where(
            session_snapshots.c.account_id == account_id,
            session_snapshots.c.observation_id == observation.observation_id,
        )
    ).first()
    if existing:
        if existing[0] != fingerprint:
            raise ValueError("Conflicting observation identity")
        assert previous is not None
        return previous
    state = advance_session(previous, observation, reference)
    sequence = connection.execute(
        select(session_snapshots.c.sequence)
        .where(session_snapshots.c.account_id == account_id)
        .order_by(session_snapshots.c.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    connection.execute(
        session_snapshots.insert().values(
            account_id=account_id,
            observation_id=observation.observation_id,
            sequence=(sequence or 0) + 1,
            fingerprint=fingerprint,
            observation=observation.model_dump(mode="json"),
            state=state.model_dump(mode="json"),
        )
    )
    return state
