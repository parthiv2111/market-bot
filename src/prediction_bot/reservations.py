"""Serialized paper reservations and explicit scenario estimates, never broker orders."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import Connection, func, select
from sqlalchemy.dialects.postgresql import insert

from prediction_bot.persistence.schema import (
    ledger_accounts,
    ledger_events,
    risk_capacity,
    risk_reservations,
)
from prediction_bot.reference_data import FrozenModel
from prediction_bot.session_accounting import read_session
from prediction_bot.sizing import (
    Amount,
    EntryTerms,
    Exposure,
    Percent,
    PortfolioCapacity,
    SizingPolicy,
    size_entry,
)
from prediction_bot.strategy import Proposal


class StressAssumption(FrozenModel):
    atr: Amount = Field(gt=0)
    exit_cost_per_share: Amount
    minimum_exit_cost: Amount
    historical_drop_pct: Percent


class StressPolicy(FrozenModel):
    simultaneous_move_pct: Percent
    atr_multiple: Decimal = Field(gt=0, allow_inf_nan=False)
    loss_limit_pct: Percent
    single_name_gap_pct: Percent
    historical_source: str = Field(min_length=1)


class StressReport(FrozenModel):
    correlated_loss: Decimal
    historical_loss: Decimal
    worst_single_name_loss: Decimal
    blocked_exit_notional: Decimal
    limit: Decimal
    passed: bool
    limitation: str = "Scenario estimates do not bound gaps or guarantee executable exits."


def stress_report(
    exposures: tuple[Exposure, ...],
    assumptions: dict[str, StressAssumption],
    policy: StressPolicy,
    e0: Decimal,
    *,
    reserved_buy_costs: Decimal = Decimal(0),
) -> StressReport:
    with localcontext() as context:
        context.prec = 60
        correlated = historical = reserved_buy_costs
        singles: dict[str, Decimal] = {}
        notional = Decimal(0)
        for exposure in exposures:
            if exposure.symbol not in assumptions:
                raise ValueError("Missing stress assumption")
            assumption = assumptions[exposure.symbol]
            value = exposure.mark * exposure.quantity
            cost = max(
                assumption.minimum_exit_cost, assumption.exit_cost_per_share * exposure.quantity
            )
            move = min(
                Decimal(1),
                max(
                    policy.simultaneous_move_pct / 100,
                    policy.atr_multiple * assumption.atr / exposure.mark,
                ),
            )
            correlated += value * move + cost
            historical += value * assumption.historical_drop_pct / 100 + cost
            singles[exposure.symbol] = (
                singles.get(exposure.symbol, Decimal(0))
                + value * policy.single_name_gap_pct / 100
                + cost
            )
            notional += value
        limit = e0 * policy.loss_limit_pct / 100
        return StressReport(
            correlated_loss=correlated,
            historical_loss=historical,
            worst_single_name_loss=max(singles.values(), default=Decimal(0)) + reserved_buy_costs,
            blocked_exit_notional=notional,
            limit=limit,
            passed=e0.is_finite() and e0 > 0 and correlated <= limit,
        )


def _lock(connection: Connection, account_id: str) -> None:
    if not account_id or len(account_id) > 128:
        raise ValueError("Invalid account identity")
    connection.execute(insert(ledger_accounts).values(id=account_id).on_conflict_do_nothing())
    connection.execute(
        select(ledger_accounts.c.id).where(ledger_accounts.c.id == account_id).with_for_update()
    ).one()


def _ledger_revision(connection: Connection, account_id: str) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(ledger_events)
            .where(ledger_events.c.account_id == account_id)
        ).scalar_one()
    )


def _active(connection: Connection, account_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            select(risk_reservations).where(
                risk_reservations.c.account_id == account_id, risk_reservations.c.status == "active"
            )
        ).mappings()
    ]


def publish_capacity(
    connection: Connection,
    account_id: str,
    portfolio: PortfolioCapacity,
    assumptions: dict[str, StressAssumption],
    sizing_policy: SizingPolicy,
    stress_policy: StressPolicy,
) -> None:
    """Publish a reconciled generation; active reservations block replacement."""
    _lock(connection, account_id)
    if _active(connection, account_id):
        raise ValueError("Resolve active reservations before replacing capacity")
    session = read_session(connection, account_id)
    if session is None or session.observed_at != portfolio.observed_at or not portfolio.reconciled:
        raise ValueError("Matching reconciled session snapshot required")
    stress_report(portfolio.exposures, assumptions, stress_policy, session.e0)
    payload = dict(
        portfolio=portfolio.model_dump(mode="json"),
        assumptions={k: v.model_dump(mode="json") for k, v in assumptions.items()},
        sizing_policy=sizing_policy.model_dump(mode="json"),
        stress_policy=stress_policy.model_dump(mode="json"),
        session=session.model_dump(mode="json"),
        ledger_revision=_ledger_revision(connection, account_id),
    )
    statement = insert(risk_capacity).values(account_id=account_id, payload=payload, valid=True)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[risk_capacity.c.account_id], set_={"payload": payload, "valid": True}
        )
    )


class ReservationResult(FrozenModel):
    status: Literal["reserved", "rejected", "released"]
    quantity: int
    reasons: tuple[str, ...]
    stress: StressReport | None = None


def reserve(
    connection: Connection,
    account_id: str,
    reservation_id: str,
    proposal: Proposal,
    terms: EntryTerms,
    *,
    now: datetime,
) -> ReservationResult:
    """Commit before relying on a reservation. Caller rollback removes the whole decision."""
    _lock(connection, account_id)
    if not reservation_id or len(reservation_id) > 128:
        raise ValueError("Invalid reservation identity")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"proposal": asdict(proposal), "terms": terms.model_dump(mode="json")},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    existing = (
        connection.execute(
            select(risk_reservations).where(
                risk_reservations.c.account_id == account_id,
                risk_reservations.c.reservation_id == reservation_id,
            )
        )
        .mappings()
        .first()
    )
    if existing:
        if existing["fingerprint"] != fingerprint:
            raise ValueError("Conflicting reservation identity")
        if existing["status"] == "released":
            return ReservationResult(
                status="released", quantity=0, reasons=("terminal_reservation",)
            )
        return ReservationResult.model_validate(existing["payload"]["result"])
    row = (
        connection.execute(select(risk_capacity).where(risk_capacity.c.account_id == account_id))
        .mappings()
        .first()
    )
    if row is None or not row["valid"]:
        return ReservationResult(
            status="rejected", quantity=0, reasons=("capacity_reconciliation_required",)
        )
    payload = row["payload"]
    session = read_session(connection, account_id)
    if (
        session is None
        or session.model_dump(mode="json") != payload["session"]
        or _ledger_revision(connection, account_id) != payload["ledger_revision"]
    ):
        return ReservationResult(
            status="rejected", quantity=0, reasons=("capacity_snapshot_changed",)
        )
    portfolio = PortfolioCapacity.model_validate(payload["portfolio"])
    policy = SizingPolicy.model_validate(payload["sizing_policy"])
    stress_policy = StressPolicy.model_validate(payload["stress_policy"])
    assumptions = {k: StressAssumption.model_validate(v) for k, v in payload["assumptions"].items()}
    active = _active(connection, account_id)
    # Managed pending orders come exclusively from this table, never twice from the base snapshot.
    pending = tuple(Exposure.model_validate(item["payload"]["exposure"]) for item in active)
    combined = PortfolioCapacity.model_validate(
        portfolio.model_dump() | {"exposures": portfolio.exposures + pending}
    )
    decision = size_entry(proposal, session, combined, terms, policy, now=now)
    if decision.action != "SIZED":
        return ReservationResult(status="rejected", quantity=0, reasons=decision.reasons)
    assert decision.stop is not None
    with localcontext() as context:
        context.prec = 60
        candidate = Exposure(
            identity="reservation:" + reservation_id,
            symbol=terms.symbol,
            sector=terms.sector,
            groups=terms.groups,
            kind="pending",
            quantity=decision.quantity,
            mark=terms.entry,
            average_entry=terms.entry,
            stop=decision.stop,
            additional_exit_risk=decision.planned_risk
            - (terms.entry - decision.stop) * decision.quantity,
            reserved_cash=decision.purchase_cost,
        )
        buy_cost, _ = terms.costs.charges(decision.quantity)
        buy_costs = buy_cost + sum(
            (Decimal(str(item["payload"]["buy_cost"])) for item in active), Decimal(0)
        )
        report = stress_report(
            combined.exposures + (candidate,),
            assumptions,
            stress_policy,
            session.e0,
            reserved_buy_costs=buy_costs,
        )
    if not report.passed:
        return ReservationResult(
            status="rejected", quantity=0, reasons=("correlated_stress_limit",), stress=report
        )
    result = ReservationResult(
        status="reserved",
        quantity=decision.quantity,
        reasons=("paper_capacity_reserved_not_dispatch_authorized",),
        stress=report,
    )
    connection.execute(
        risk_reservations.insert().values(
            account_id=account_id,
            reservation_id=reservation_id,
            fingerprint=fingerprint,
            status="active",
            payload={
                "result": result.model_dump(mode="json"),
                "exposure": candidate.model_dump(mode="json"),
                "buy_cost": str(buy_cost),
                "decision": asdict(decision)
                | {
                    "planned_risk": str(decision.planned_risk),
                    "purchase_cost": str(decision.purchase_cost),
                    "stop": str(decision.stop),
                },
                "stress_policy": payload["stress_policy"],
                "assumptions": payload["assumptions"],
            },
            terminal_evidence=None,
        )
    )
    return result


class TerminalConfirmation(FrozenModel):
    state: Literal["filled", "cancelled", "rejected"]
    confirmed: Literal[True]
    filled_quantity: int = Field(ge=0, strict=True)
    evidence_reference: str = Field(min_length=1)


def release(
    connection: Connection, account_id: str, reservation_id: str, confirmation: TerminalConfirmation
) -> None:
    _lock(connection, account_id)
    row = (
        connection.execute(
            select(risk_reservations).where(
                risk_reservations.c.account_id == account_id,
                risk_reservations.c.reservation_id == reservation_id,
            )
        )
        .mappings()
        .one()
    )
    evidence = confirmation.model_dump(mode="json")
    if row["status"] == "released":
        if row["terminal_evidence"] != evidence:
            raise ValueError("Conflicting terminal confirmation")
        return
    quantity = row["payload"]["result"]["quantity"]
    if (
        confirmation.filled_quantity > quantity
        or (confirmation.state == "filled" and confirmation.filled_quantity != quantity)
        or (confirmation.state == "rejected" and confirmation.filled_quantity)
    ):
        raise ValueError("Invalid terminal fill quantity")
    if confirmation.filled_quantity:
        # Filled exposure/cash must be reconciled before any capacity can be reused.
        connection.execute(
            risk_capacity.update()
            .where(risk_capacity.c.account_id == account_id)
            .values(valid=False)
        )
    connection.execute(
        risk_reservations.update()
        .where(
            risk_reservations.c.account_id == account_id,
            risk_reservations.c.reservation_id == reservation_id,
        )
        .values(status="released", terminal_evidence=evidence)
    )
