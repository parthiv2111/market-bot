from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from prediction_bot.ledger import LedgerEvent, replay


def event(identifier, kind, **values):
    return LedgerEvent(
        event_id=str(identifier),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=identifier),
        kind=kind,
        source="manual",
        **values,
    )


def history():
    return (
        event(1, "deposit", amount="1000"),
        event(
            2, "buy", instrument_id="A", quantity=3, price="100", fee="1", order_id="partial-order"
        ),
        event(
            3, "buy", instrument_id="A", quantity=2, price="110", fee="1", order_id="partial-order"
        ),
        event(4, "sell", instrument_id="A", quantity=2, price="120", fee="2"),
        event(5, "mark", instrument_id="A", price="115"),
    )


def test_partial_fills_costs_and_net_pnl_reconcile():
    state = replay(history())
    assert state.cash == 716
    assert state.holdings["A"].quantity == 3
    assert state.holdings["A"].cost_basis == Decimal("313.2")
    assert state.realized_pnl == Decimal("29.2")
    assert state.unrealized_pnl == Decimal("31.8")
    assert state.equity == 1061
    assert state.fees == 4
    assert state.equity - state.net_contributions == state.realized_pnl + state.unrealized_pnl


def test_close_position_flushes_basis_without_double_counting_fees():
    events = history() + (
        event(6, "sell", instrument_id="A", quantity=3, price="115", fee="1"),
        event(7, "withdrawal", amount="60"),
    )
    state = replay(events)
    assert state.cash == state.equity == 1000
    assert state.net_contributions == 940
    assert state.realized_pnl == 60
    assert state.unrealized_pnl == state.holdings["A"].cost_basis == 0
    assert state.fees == 5


def test_retries_are_noops_but_conflicts_fail():
    events = history()
    assert replay(events + (events[1],)) == replay(events)
    with pytest.raises(ValueError, match="Conflicting"):
        replay(events + (events[1].model_copy(update={"fee": Decimal(2)}),))


@pytest.mark.parametrize(
    "extra,message",
    [
        (event(6, "sell", instrument_id="A", quantity=4, price="100"), "inventory"),
        (event(6, "buy", instrument_id="B", quantity=100, price="100"), "cash"),
        (event(6, "withdrawal", amount="1000"), "cash"),
        (event(6, "mark", instrument_id="B", price="100"), "unowned"),
        (event(0, "deposit", amount="1"), "chronological"),
    ],
)
def test_invalid_transitions(extra, message):
    with pytest.raises(ValueError, match=message):
        replay(history() + (extra,))


def test_fractional_basis_rounding_and_decimal_context_independence():
    events = (
        event(1, "deposit", amount="100"),
        event(2, "buy", instrument_id="A", quantity=3, price="1", fee="1"),
    )
    events += tuple(event(i, "sell", instrument_id="A", quantity=1, price="2") for i in range(3, 6))
    with localcontext() as context:
        context.prec = 5
        state = replay(events)
    assert state.realized_pnl == 2
    assert state.cash == 102
    assert state.holdings["A"].cost_basis == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"price": "NaN"},
        {"fee": "-1"},
        {"quantity": 1.5},
        {"price": "0.0000001"},
        {"amount": "10"},
        {"occurred_at": datetime(2026, 1, 1)},
    ],
)
def test_malformed_fills_rejected(changes):
    payload = history()[1].model_dump() | changes
    with pytest.raises(ValidationError):
        LedgerEvent(**payload)


def test_multiple_instruments_and_manual_trade_source():
    state = replay(
        history() + (event(6, "buy", instrument_id="B", quantity=1, price="10", fee="1"),)
    )
    assert state.equity == 1060
    assert state.holdings["A"].quantity == 3
    assert state.holdings["B"].quantity == 1
    assert history()[1].source == "manual"
