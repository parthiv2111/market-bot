from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from prediction_bot.reference_data import ExchangeDay, InstrumentAlias, ReferenceBundle
from prediction_bot.regimes import RegimeDecision
from prediction_bot.strategy import InventorySnapshot, ModelSignal, StrategyPolicy, propose


def instant(day, hour=10):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


@pytest.fixture
def inputs():
    signal = ModelSignal(
        symbol="SYNTH",
        signal_at=instant(2),
        generated_at=instant(2),
        model_version="fixture",
        probability_positive_net_return=0.7,
        reference_close=Decimal(100),
        atr=Decimal(2),
    )
    inventory = InventorySnapshot(
        symbol="SYNTH", owned_quantity=10, reserved_sell_quantity=3, observed_at=instant(2)
    )
    policy = StrategyPolicy(
        buy_probability=0.7,
        sell_probability=0.3,
        entry_band_bps=Decimal(100),
        stop_atr_multiple=Decimal(2),
        max_signal_age_seconds=172800,
        max_inventory_age_seconds=172800,
    )
    reference = ReferenceBundle(
        source="synthetic",
        published_at=instant(1),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 8),
        aliases=(
            InstrumentAlias(
                instrument_id="stable",
                exchange_id="id",
                symbol="SYNTH",
                valid_from=date(2026, 1, 1),
                valid_to=date(2027, 1, 1),
            ),
        ),
        days=tuple(
            ExchangeDay(
                session_date=date(2026, 1, d),
                status="open",
                reason="synthetic",
                opens_at=instant(d, 4),
                closes_at=instant(d),
            )
            for d in range(2, 9)
        ),
    )
    regime = RegimeDecision(
        date(2026, 1, 2),
        instant(2),
        "bull",
        "bull",
        "normal",
        True,
        "fixture-rule",
        "{}",
        {"equity.symbol": "SYNTH"},
        (),
    )
    return signal, regime, inventory, reference, policy


def test_buy_contract(inputs):
    proposal = propose(*inputs, now=instant(2))
    assert proposal.action == "BUY"
    assert (proposal.entry_low, proposal.entry_high, proposal.invalidation_stop) == (99, 101, 96)
    assert proposal.valid_from == instant(3, 4)
    assert proposal.expires_at == instant(3)
    assert proposal.planned_exit_at == instant(7)
    assert proposal.holding_sessions == 5
    assert proposal.quantity is None and not proposal.reduce_only
    assert proposal.mode == "paper" and "risk_approval_required" in proposal.reasons
    assert proposal.policy_version == inputs[-1].version
    assert not hasattr(proposal, "execute")


@pytest.mark.parametrize("owned,reserved,expected", [(10, 3, 7), (0, 0, 0), (10, 10, 0)])
def test_sell_only_available_inventory_even_without_entry_regime(inputs, owned, reserved, expected):
    signal, _, inventory, reference, policy = inputs
    signal = signal.model_copy(update={"probability_positive_net_return": 0.3})
    inventory = inventory.model_copy(
        update={"owned_quantity": owned, "reserved_sell_quantity": reserved}
    )
    result = propose(signal, None, inventory, reference, policy, now=instant(2))
    assert result.action == ("SELL" if expected else "NO_TRADE")
    assert result.quantity == (expected or None)
    assert result.reduce_only == bool(expected)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"generated_at": instant(3)}, "future_signal"),
        ({"signal_at": instant(2, 9)}, "not_completed_session_signal"),
        ({"symbol": "MISSING"}, "reference_coverage_unavailable"),
        ({"probability_positive_net_return": 0.5}, "probability_in_neutral_band"),
        ({"atr": Decimal(100)}, "invalid_invalidation_stop"),
        ({"atr": Decimal("0.1")}, "invalid_invalidation_stop"),
    ],
)
def test_rejections(inputs, changes, reason):
    signal, regime, inventory, reference, policy = inputs
    result = propose(
        signal.model_copy(update=changes), regime, inventory, reference, policy, now=instant(2)
    )
    assert result.action == "NO_TRADE" and reason in result.reasons
    assert result.quantity is None and result.entry_low is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"entry_eligible": False}, "regime_blocks_entries"),
        ({"regime": "unknown"}, "unknown_regime"),
        ({"as_of": instant(1)}, "regime_signal_mismatch"),
        ({"inputs": {"equity.symbol": "OTHER"}}, "regime_signal_mismatch"),
    ],
)
def test_regime_rejections(inputs, change, reason):
    signal, regime, inventory, reference, policy = inputs
    result = propose(
        signal, replace(regime, **change), inventory, reference, policy, now=instant(2)
    )
    assert result.reasons == (reason,)


def test_age_and_expiry_boundaries(inputs):
    signal, regime, inventory, reference, policy = inputs
    short = policy.model_copy(update={"max_signal_age_seconds": 60})
    assert (
        propose(
            signal, regime, inventory, reference, short, now=instant(2) + timedelta(seconds=60)
        ).action
        == "BUY"
    )
    assert (
        "stale_signal"
        in propose(
            signal, regime, inventory, reference, short, now=instant(2) + timedelta(seconds=61)
        ).reasons
    )
    assert propose(*inputs, now=instant(3)).reasons == ("proposal_expired",)
    assert (
        "stale_or_future_inventory"
        in propose(
            signal,
            regime,
            inventory.model_copy(update={"observed_at": instant(3)}),
            reference,
            policy,
            now=instant(2),
        ).reasons
    )


@pytest.mark.parametrize("probability", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_probability_rejected(inputs, probability):
    data = inputs[0].model_dump()
    data["probability_positive_net_return"] = probability
    with pytest.raises(ValidationError):
        ModelSignal(**data)


def test_invalid_timing_inventory_and_policy(inputs):
    signal, _, inventory, _, policy = inputs
    for changes in ({"signal_at": datetime(2026, 1, 2)}, {"generated_at": instant(1)}):
        with pytest.raises(ValidationError):
            ModelSignal(**(signal.model_dump() | changes))
    with pytest.raises(ValidationError):
        InventorySnapshot(**(inventory.model_dump() | {"reserved_sell_quantity": 11}))
    with pytest.raises(ValidationError):
        StrategyPolicy(**(policy.model_dump() | {"sell_probability": 0.7}))


def test_missing_future_calendar_fails_closed(inputs):
    signal, regime, inventory, reference, policy = inputs
    reference = reference.model_copy(
        update={"days": reference.days[:2], "coverage_end": date(2026, 1, 3)}
    )
    assert propose(signal, regime, inventory, reference, policy, now=instant(2)).reasons == (
        "reference_coverage_unavailable",
    )
