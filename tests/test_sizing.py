from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from prediction_bot.session_accounting import SessionState
from prediction_bot.sizing import (
    Costs,
    EntryTerms,
    Exposure,
    PortfolioCapacity,
    SizingPolicy,
    size_entry,
)
from prediction_bot.strategy import Proposal

NOW = datetime(2026, 1, 26, 5, tzinfo=UTC)


def build_inputs():
    session = SessionState(
        observed_at=NOW,
        session_date=NOW.date(),
        week=(2026, 5),
        month=(2026, 1),
        e0=1000000,
        week_e0=1000000,
        month_e0=1000000,
        day_flow_base=1000000,
        week_flow_base=1000000,
        month_flow_base=1000000,
        cumulative_external_flows=1000000,
        equity=1000000,
        daily_pnl=0,
        weekly_pnl=0,
        monthly_pnl=0,
        session_high=0,
        unit_index=1,
        lifetime_peak=1,
        drawdown=0,
        session_entries=0,
        completed_campaigns=0,
        consecutive_losses=0,
        latch_reasons=(),
        reference_version="fixture",
    )
    proposal = Proposal(
        "BUY",
        "A",
        NOW - timedelta(days=1),
        NOW,
        "model",
        "regime",
        "policy",
        NOW,
        NOW + timedelta(hours=1),
        Decimal(990),
        Decimal(1000),
        Decimal(980),
        5,
        NOW + timedelta(days=5),
        None,
        False,
        (),
    )
    portfolio = PortfolioCapacity(
        observed_at=NOW, reconciled=True, cash=1000000, remaining_risk_budget=7500, exposures=()
    )
    terms = EntryTerms(
        symbol="A",
        sector="IT",
        groups=("growth", "index"),
        entry=1000,
        lot_size=1,
        liquidity_quantity=10000,
        observed_at=NOW,
        costs=Costs(
            buy_per_share=1,
            sell_per_share=1,
            buy_minimum=0,
            sell_minimum=0,
            slippage_gap_per_share=3,
        ),
    )
    policy = SizingPolicy(
        per_trade_risk_pct="0.25",
        symbol_pct=10,
        sector_pct=25,
        group_pct=25,
        gross_pct=60,
        max_positions=6,
        max_age_seconds=60,
    )
    return proposal, session, portfolio, terms, policy


@pytest.fixture
def inputs():
    return build_inputs()


def exposure(**changes):
    values = dict(
        identity="held",
        symbol="B",
        sector="IT",
        groups=("growth",),
        kind="held",
        quantity=200,
        mark=1000,
        average_entry=1000,
        stop=990,
        additional_exit_risk=0,
        reserved_cash=0,
    )
    return Exposure(**(values | changes))


def test_plan_worked_example(inputs):
    result = size_entry(*inputs, now=NOW)
    assert result.quantity == 100
    assert result.planned_risk == 2500
    assert result.purchase_cost == 100100
    assert result.stop == 980 and result.action == "SIZED"


@pytest.mark.parametrize(
    "budget,lot,expected", [(24, 1, 0), (25, 1, 1), (249, 10, 0), (250, 10, 10), (499, 10, 10)]
)
def test_one_lot_and_round_down(inputs, budget, lot, expected):
    p, s, portfolio, terms, policy = inputs
    result = size_entry(
        p,
        s,
        portfolio.model_copy(update={"remaining_risk_budget": Decimal(budget)}),
        terms.model_copy(update={"lot_size": lot}),
        policy,
        now=NOW,
    )
    assert result.quantity == expected
    assert result.action == ("SIZED" if expected else "NO_TRADE")


@pytest.mark.parametrize(
    "limits,held,expected",
    [
        ({}, (exposure(),), 50),  # sector and overlapping group
        ({"sector_pct": Decimal(100)}, (exposure(sector="OTHER"),), 50),
        (
            {"sector_pct": Decimal(100), "group_pct": Decimal(100), "gross_pct": Decimal(21)},
            (exposure(sector="OTHER", groups=("other",)),),
            10,
        ),
        ({}, (exposure(symbol="A", quantity=90, stop=980, groups=("growth", "index")),), 10),
    ],
)
def test_exposure_capacities(inputs, limits, held, expected):
    p, s, portfolio, terms, policy = inputs
    result = size_entry(
        p,
        s,
        portfolio.model_copy(update={"exposures": held}),
        terms,
        policy.model_copy(update=limits),
        now=NOW,
    )
    assert result.quantity == expected


def test_pending_cash_heat_and_position_count(inputs):
    p, s, portfolio, terms, policy = inputs
    pending = exposure(
        kind="pending", quantity=1, mark=1000, reserved_cash=1001, additional_exit_risk=2
    )
    portfolio = portfolio.model_copy(
        update={
            "exposures": (pending,),
            "cash": Decimal(2002),
            "remaining_risk_budget": Decimal(37),
        }
    )
    result = size_entry(p, s, portfolio, terms, policy, now=NOW)
    assert result.quantity == 1 and result.planned_risk == 25
    assert size_entry(
        p, s, portfolio, terms, policy.model_copy(update={"max_positions": 1}), now=NOW
    ).reasons == ("position_count_capacity",)


def test_minimum_costs_recomputed_exactly(inputs):
    p, s, portfolio, terms, policy = inputs
    terms = terms.model_copy(
        update={
            "costs": Costs(
                buy_per_share=0,
                sell_per_share=0,
                buy_minimum=100,
                sell_minimum=100,
                slippage_gap_per_share=0,
            )
        }
    )
    portfolio = portfolio.model_copy(update={"remaining_risk_budget": Decimal(250)})
    result = size_entry(p, s, portfolio, terms, policy, now=NOW)
    assert result.quantity == 2 and result.planned_risk == 240 and result.purchase_cost == 2100


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"average_entry": 1001}, "averaging_down_forbidden"),
        ({"mark": 999}, "averaging_down_forbidden"),
        ({"stop": 981}, "stop_widening_forbidden"),
        ({"sector": "OTHER"}, "classification_mismatch"),
    ],
)
def test_no_averaging_down_or_stop_widening(inputs, changes, reason):
    p, s, portfolio, terms, policy = inputs
    held = (
        exposure(symbol="A", quantity=1, stop=980, groups=terms.groups, **changes)
        if "stop" not in changes
        else exposure(symbol="A", quantity=1, groups=terms.groups, **changes)
    )
    result = size_entry(
        p, s, portfolio.model_copy(update={"exposures": (held,)}), terms, policy, now=NOW
    )
    assert result.reasons == (reason,) and result.quantity == 0


def test_stale_latched_expired_and_missing_inputs(inputs):
    p, s, portfolio, terms, policy = inputs
    assert size_entry(p, s, portfolio, terms, policy, now=NOW + timedelta(seconds=61)).quantity == 0
    assert (
        size_entry(
            p, s.model_copy(update={"latch_reasons": ("stop",)}), portfolio, terms, policy, now=NOW
        ).quantity
        == 0
    )
    assert (
        size_entry(replace(p, expires_at=NOW), s, portfolio, terms, policy, now=NOW).quantity == 0
    )
    assert (
        size_entry(
            replace(p, invalidation_stop=None), s, portfolio, terms, policy, now=NOW
        ).quantity
        == 0
    )
    assert (
        size_entry(
            p, s, portfolio, terms.model_copy(update={"liquidity_quantity": 0}), policy, now=NOW
        ).quantity
        == 0
    )


def test_nonfinite_missing_and_pending_reservation_validation(inputs):
    with pytest.raises(ValidationError):
        SizingPolicy(per_trade_risk_pct="NaN")
    with pytest.raises(ValidationError):
        exposure(kind="pending", reserved_cash=0)
    with pytest.raises(ValidationError):
        EntryTerms(**(inputs[3].model_dump() | {"groups": ()}))
