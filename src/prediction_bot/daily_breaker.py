"""Durable daily-loss latch and paper execution intents; no broker calls."""

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from prediction_bot.ledger import read_ledger
from prediction_bot.persistence.schema import (
    daily_controls,
    risk_capacity,
    risk_reservations,
    stop_intents,
)

if TYPE_CHECKING:
    from prediction_bot.session_accounting import SessionState


def loss_amounts(
    e0: Decimal, percentage: Decimal, pnl: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    if (
        not all(v.is_finite() for v in (e0, percentage, pnl))
        or e0 <= 0
        or not 0 < percentage <= 100
    ):
        raise ValueError("Positive finite E0 and loss percentage required")
    with localcontext() as context:
        context.prec = 60
        cutoff = e0 * percentage / 100
        loss = max(Decimal(0), -pnl)
        return cutoff, loss, max(Decimal(0), loss - cutoff)


def bind_daily_limit(
    connection: Connection, account_id: str, session: "SessionState", percentage: Decimal
) -> None:
    """Caller holds account lock; same-session policy cannot change."""
    loss_amounts(session.e0, percentage, session.daily_pnl)
    row = (
        connection.execute(
            select(daily_controls).where(
                daily_controls.c.account_id == account_id,
                daily_controls.c.session_date == session.session_date,
            )
        )
        .mappings()
        .first()
    )
    if row:
        if row["percentage"] != percentage or row["e0"] != session.e0:
            raise ValueError("Daily policy and E0 are frozen for the session")
    else:
        connection.execute(
            daily_controls.insert().values(
                account_id=account_id,
                session_date=session.session_date,
                percentage=percentage,
                e0=session.e0,
                triggered=False,
                payload={},
            )
        )
    evaluate_daily_loss(connection, account_id, session)


def evaluate_daily_loss(connection: Connection, account_id: str, session: "SessionState") -> bool:
    """Evaluate under account lock; unconfigured accounts and latched stops fail closed."""
    row = (
        connection.execute(
            select(daily_controls).where(
                daily_controls.c.account_id == account_id,
                daily_controls.c.session_date == session.session_date,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return False
    cutoff, loss, overshoot = loss_amounts(row["e0"], row["percentage"], session.daily_pnl)
    if loss >= cutoff and not row["triggered"]:
        payload = {
            "reason": "DAILY_LOSS_LIMIT",
            "state": "DAY_STOP",
            "triggered_at": session.observed_at.isoformat(),
            "e0": str(row["e0"]),
            "percentage": str(row["percentage"]),
            "pnl": str(session.daily_pnl),
            "cutoff": str(cutoff),
            "overshoot": str(overshoot),
        }
        connection.execute(
            daily_controls.update()
            .where(
                daily_controls.c.account_id == account_id,
                daily_controls.c.session_date == session.session_date,
            )
            .values(triggered=True, payload=payload)
        )
    stopped = connection.execute(
        select(daily_controls.c.session_date)
        .where(daily_controls.c.account_id == account_id, daily_controls.c.triggered.is_(True))
        .order_by(daily_controls.c.session_date)
    ).first()
    if not stopped:
        return True
    connection.execute(
        risk_capacity.update().where(risk_capacity.c.account_id == account_id).values(valid=False)
    )
    for entry_row in connection.execute(
        select(risk_reservations.c.reservation_id).where(
            risk_reservations.c.account_id == account_id, risk_reservations.c.status == "active"
        )
    ):
        identifier = str(entry_row[0])
        connection.execute(
            insert(stop_intents)
            .values(
                account_id=account_id,
                session_date=stopped[0],
                target="entry:" + identifier,
                kind="CANCEL_ENTRY",
                payload={"reservation_id": identifier, "reason": "DAY_STOP"},
            )
            .on_conflict_do_nothing()
        )
    holdings = read_ledger(connection, account_id).holdings
    connection.execute(
        insert(stop_intents)
        .values(
            account_id=account_id,
            session_date=stopped[0],
            target="managed-account",
            kind="LIQUIDATE_ACCOUNT",
            payload={
                "reason": "DAY_STOP",
                "instruction": (
                    "Reconcile current holdings and late fills; reduce only when executable; "
                    "preserve protective exits until confirmed closed."
                ),
                "initial_holdings": {
                    key: value.quantity for key, value in holdings.items() if value.quantity
                },
            },
        )
        .on_conflict_do_nothing()
    )
    return False
