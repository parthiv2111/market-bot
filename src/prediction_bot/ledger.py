"""Append-only paper accounting with serialized, idempotent event ingestion."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import Field, model_validator
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert

from prediction_bot.data_quality import _aware
from prediction_bot.persistence.schema import ledger_accounts, ledger_events
from prediction_bot.reference_data import FrozenModel

Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6, allow_inf_nan=False)]
ZERO = Decimal(0)
UNIT = Decimal("0.000001")


class LedgerEvent(FrozenModel):
    event_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    kind: Literal["deposit", "withdrawal", "buy", "sell", "mark"]
    source: Literal["manual", "paper", "import"]
    instrument_id: str | None = Field(default=None, min_length=1)
    quantity: int = Field(default=0, ge=0, le=1_000_000_000, strict=True)
    price: Money = ZERO
    fee: Money = ZERO
    amount: Money = ZERO
    order_id: str | None = None

    @model_validator(mode="after")
    def validate_event(self) -> "LedgerEvent":
        _aware(self.occurred_at)
        if self.kind in ("deposit", "withdrawal"):
            if (
                self.amount <= 0
                or self.instrument_id
                or self.quantity
                or self.price
                or self.fee
                or self.order_id
            ):
                raise ValueError("Cash flows require only a positive amount")
        elif self.kind == "mark":
            if (
                not self.instrument_id
                or self.price <= 0
                or self.quantity
                or self.fee
                or self.amount
                or self.order_id
            ):
                raise ValueError("Marks require only an instrument and positive price")
        elif not self.instrument_id or self.quantity <= 0 or self.price <= 0 or self.amount:
            raise ValueError(
                "Fills require instrument, positive quantity and price, and no cash-flow amount"
            )
        return self


@dataclass(frozen=True)
class Holding:
    quantity: int
    cost_basis: Decimal
    mark: Decimal


@dataclass(frozen=True)
class LedgerState:
    cash: Decimal
    net_contributions: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    fees: Decimal
    holdings: dict[str, Holding]
    as_of: datetime | None


def replay(events: tuple[LedgerEvent, ...]) -> LedgerState:
    """Weighted-average cost, inclusive buy fees; six-decimal basis allocation."""
    cash = contributions = realized = fees = ZERO
    holdings: dict[str, Holding] = {}
    seen: dict[str, LedgerEvent] = {}
    last = None
    with localcontext() as context:
        context.prec = 60
        for event in events:
            if event.event_id in seen:
                if seen[event.event_id] != event:
                    raise ValueError("Conflicting event identity")
                continue
            if last is not None and event.occurred_at < last:
                raise ValueError(
                    "Events must be chronological; backdated entries require reconciliation"
                )
            seen[event.event_id] = event
            last = event.occurred_at
            if event.kind in ("deposit", "withdrawal"):
                delta = event.amount if event.kind == "deposit" else -event.amount
                if cash + delta < 0:
                    raise ValueError("Insufficient cash")
                cash += delta
                contributions += delta
                continue
            assert event.instrument_id is not None
            old = holdings.get(event.instrument_id, Holding(0, ZERO, event.price))
            gross = event.price * event.quantity
            if event.kind == "buy":
                cost = gross + event.fee
                if cost > cash:
                    raise ValueError("Insufficient cash")
                cash -= cost
                holdings[event.instrument_id] = Holding(
                    old.quantity + event.quantity, old.cost_basis + cost, event.price
                )
            elif event.kind == "sell":
                if event.quantity > old.quantity:
                    raise ValueError("Insufficient owned inventory")
                basis = (
                    old.cost_basis
                    if event.quantity == old.quantity
                    else (old.cost_basis * event.quantity / old.quantity).quantize(
                        UNIT, rounding=ROUND_HALF_EVEN
                    )
                )
                proceeds = gross - event.fee
                if cash + proceeds < 0:
                    raise ValueError("Insufficient cash for fees")
                cash += proceeds
                realized += proceeds - basis
                holdings[event.instrument_id] = Holding(
                    old.quantity - event.quantity, old.cost_basis - basis, event.price
                )
            else:
                if not old.quantity:
                    raise ValueError("Cannot mark an unowned instrument")
                holdings[event.instrument_id] = Holding(old.quantity, old.cost_basis, event.price)
            fees += event.fee
        value = sum((h.mark * h.quantity for h in holdings.values()), ZERO)
        basis = sum((h.cost_basis for h in holdings.values()), ZERO)
        equity = cash + value
        unrealized = value - basis
        if equity - contributions != realized + unrealized:
            raise ValueError("Accounting reconciliation failed")
        return LedgerState(cash, contributions, realized, unrealized, equity, fees, holdings, last)


def read_ledger(connection: Connection, account_id: str) -> LedgerState:
    rows = connection.execute(
        select(ledger_events.c.payload)
        .where(ledger_events.c.account_id == account_id)
        .order_by(ledger_events.c.sequence)
    )
    return replay(tuple(LedgerEvent.model_validate(row[0]) for row in rows))


def ingest_event(connection: Connection, account_id: str, event: LedgerEvent) -> LedgerState:
    """Caller commits/rolls back; account row lock lasts until transaction end."""
    if not account_id or len(account_id) > 128:
        raise ValueError("Account identity must contain 1 to 128 characters")
    connection.execute(insert(ledger_accounts).values(id=account_id).on_conflict_do_nothing())
    connection.execute(
        select(ledger_accounts.c.id).where(ledger_accounts.c.id == account_id).with_for_update()
    ).one()
    rows = (
        connection.execute(
            select(ledger_events)
            .where(ledger_events.c.account_id == account_id)
            .order_by(ledger_events.c.sequence)
        )
        .mappings()
        .all()
    )
    events = tuple(LedgerEvent.model_validate(row["payload"]) for row in rows)
    state = replay(events + (event,))
    if not any(old.event_id == event.event_id for old in events):
        connection.execute(
            ledger_events.insert().values(
                account_id=account_id,
                event_id=event.event_id,
                sequence=len(events) + 1,
                payload=event.model_dump(mode="json"),
            )
        )
    return state
