from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_sizing import exposure

from prediction_bot.reservations import (
    StressAssumption,
    StressPolicy,
    TerminalConfirmation,
    stress_report,
)


def stress_inputs():
    assumption = StressAssumption(
        atr=10, exit_cost_per_share=1, minimum_exit_cost=0, historical_drop_pct=30
    )
    policy = StressPolicy(
        simultaneous_move_pct=5,
        atr_multiple=3,
        loss_limit_pct=2,
        single_name_gap_pct=20,
        historical_source="synthetic scenario, not historical evidence",
    )
    return assumption, policy


def test_stress_scenarios_and_costs_once():
    assumption, policy = stress_inputs()
    position = exposure(quantity=100)
    report = stress_report(
        (position,), {"B": assumption}, policy, Decimal(1000000), reserved_buy_costs=Decimal(100)
    )
    assert report.correlated_loss == 5200
    assert report.historical_loss == 30200
    assert report.worst_single_name_loss == 20200
    assert report.blocked_exit_notional == 100000
    assert report.passed and "do not bound" in report.limitation


def test_atr_and_boundary_gate():
    assumption, policy = stress_inputs()
    assumption = assumption.model_copy(
        update={"atr": Decimal(100), "exit_cost_per_share": Decimal(0)}
    )
    position = exposure(quantity=100)
    policy = policy.model_copy(update={"loss_limit_pct": Decimal(3)})
    report = stress_report((position,), {"B": assumption}, policy, Decimal(1000000))
    assert report.correlated_loss == 30000 and report.passed
    assert not stress_report((position,), {"B": assumption}, policy, Decimal(999999)).passed


def test_missing_assumptions_and_nonterminal_confirmation_rejected():
    _, policy = stress_inputs()
    with pytest.raises(ValueError, match="Missing stress"):
        stress_report((exposure(),), {}, policy, Decimal(1000000))
    for state in ("unknown", "cancel_pending", "partial"):
        with pytest.raises(ValidationError):
            TerminalConfirmation(
                state=state, confirmed=True, filled_quantity=0, evidence_reference="fixture"
            )
    with pytest.raises(ValidationError):
        TerminalConfirmation(
            state="cancelled", confirmed=False, filled_quantity=0, evidence_reference="fixture"
        )
