from decimal import Decimal

import pytest
from test_sizing import NOW, build_inputs

from prediction_bot.daily_breaker import loss_amounts
from prediction_bot.sizing import size_entry


@pytest.mark.parametrize(
    "pnl,triggered,overshoot",
    [
        ("-999", False, 0),
        ("-1000", True, 0),
        ("-1250", True, 250),
        ("0", False, 0),
        ("500", False, 0),
    ],
)
def test_exact_loss_boundary(pnl, triggered, overshoot):
    cutoff, loss, excess = loss_amounts(Decimal(100000), Decimal(1), Decimal(pnl))
    assert cutoff == 1000
    assert (loss >= cutoff) == triggered
    assert excess == overshoot


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "101"])
def test_invalid_loss_policy(value):
    with pytest.raises(ValueError):
        loss_amounts(Decimal(100000), Decimal(value), Decimal(0))


def test_daily_loss_headroom_limits_new_heat():
    proposal, session, portfolio, terms, policy = build_inputs()
    session = session.model_copy(update={"daily_pnl": Decimal(-9975)})
    assert size_entry(proposal, session, portfolio, terms, policy, now=NOW).quantity == 1
    session = session.model_copy(update={"daily_pnl": Decimal(-10000)})
    assert size_entry(proposal, session, portfolio, terms, policy, now=NOW).quantity == 0
