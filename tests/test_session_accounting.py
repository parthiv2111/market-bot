from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from prediction_bot.reference_data import ExchangeDay, ReferenceBundle
from prediction_bot.session_accounting import EquityObservation, SessionState, advance_session


def reference():
    start = date(2026, 1, 26)
    days = tuple(start + timedelta(days=i) for i in range(10))
    return ReferenceBundle(
        source="synthetic",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_start=days[0],
        coverage_end=days[-1],
        aliases=(),
        days=tuple(
            ExchangeDay(
                session_date=d,
                status="open",
                reason="fixture",
                opens_at=datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=4),
                closes_at=datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=10),
            )
            if d.weekday() < 5
            else ExchangeDay(session_date=d, status="closed", reason="fixture")
            for d in days
        ),
    )


def observation(identifier, day=26, hour=5, equity="1000", flows="1000", **changes):
    month = 1 if day >= 26 else 2
    return EquityObservation(
        observation_id=identifier,
        observed_at=datetime(2026, month, day, hour, tzinfo=UTC),
        equity=Decimal(equity),
        cumulative_external_flows=Decimal(flows),
        reconciled=True,
        fresh_marks=True,
        **changes,
    )


def start():
    return advance_session(None, observation("open", hour=4, session_open=True), reference())


def test_deposit_withdrawal_cannot_erase_losses_peak_or_latches():
    state = advance_session(start(), observation("peak", equity="1100", new_entries=1), reference())
    state = advance_session(
        state,
        observation(
            "loss", hour=6, equity="990", campaign_result="loss", latch_reasons=("weekly_stop",)
        ),
        reference(),
    )
    assert state.daily_pnl == -10 and state.drawdown == Decimal("0.1")
    peak = state.lifetime_peak
    state = advance_session(
        state, observation("deposit", hour=7, equity="1990", flows="2000"), reference()
    )
    assert state.e0 == state.week_e0 == state.month_e0 == 1000
    assert state.daily_pnl == state.weekly_pnl == state.monthly_pnl == -10
    assert state.drawdown == Decimal("0.1") and state.lifetime_peak == peak
    state = advance_session(
        state, observation("withdraw", hour=8, equity="1490", flows="1500"), reference()
    )
    assert state.daily_pnl == -10 and state.drawdown == Decimal("0.1")
    assert state.session_entries == state.completed_campaigns == state.consecutive_losses == 1
    assert "weekly_stop" in state.latch_reasons
    assert "funding_reconciliation_required" in state.latch_reasons
    assert SessionState.model_validate_json(state.model_dump_json()) == state


def test_day_rollover_preserves_period_baselines_and_stops():
    state = advance_session(
        start(),
        observation(
            "loss",
            equity="900",
            new_entries=2,
            campaign_result="loss",
            latch_reasons=("monthly_stop",),
        ),
        reference(),
    )
    state = advance_session(
        state, observation("tomorrow", day=27, hour=4, equity="900", session_open=True), reference()
    )
    assert state.e0 == 900 and state.daily_pnl == 0
    assert state.week_e0 == state.month_e0 == 1000
    assert state.weekly_pnl == state.monthly_pnl == -100
    assert state.session_entries == state.completed_campaigns == 0
    assert state.consecutive_losses == 1
    assert state.latch_reasons == ("monthly_stop",)
    assert state.drawdown == Decimal("0.1")


def test_week_month_boundaries_at_exchange_open():
    state = advance_session(
        start(), observation("loss", equity="900", latch_reasons=("weekly_stop",)), reference()
    )
    for day in (27, 28, 29, 30, 2):
        state = advance_session(
            state,
            observation(str(day), day=day, hour=4, equity="900", session_open=True),
            reference(),
        )
    assert state.week == (2026, 6) and state.month == (2026, 2)
    assert state.week_e0 == state.month_e0 == 900
    assert state.weekly_pnl == state.monthly_pnl == 0
    assert state.drawdown == Decimal("0.1")
    assert state.latch_reasons == ("weekly_stop",)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"hour": 0}, "outside"),
        ({"day": 31}, "Not an exchange"),
        ({"day": 27}, "opening valuation"),
        ({"day": 28, "hour": 4, "session_open": True}, "intervening"),
        ({"equity": "1100", "flows": "1200"}, "before/after"),
    ],
)
def test_invalid_transition_does_not_mutate_state(changes, message):
    state = start()
    with pytest.raises(ValueError, match=message):
        advance_session(state, observation("bad", **changes), reference())
    assert state.equity == 1000 and state.unit_index == 1


@pytest.mark.parametrize("field", ["fresh_marks", "reconciled"])
def test_stale_unreconciled_input_cannot_raise_high_water_mark(field):
    state = start()
    bad = observation("bad", equity="9999").model_copy(update={field: False})
    with pytest.raises(ValueError, match="fresh marks"):
        advance_session(state, bad, reference())
    assert state.session_high == 0 and state.lifetime_peak == 1


def test_zero_equity_records_total_drawdown_and_blocks_recapitalization():
    state = advance_session(start(), observation("zero", equity="0"), reference())
    assert state.drawdown == 1 and "zero_equity" in state.latch_reasons
    with pytest.raises(ValueError, match="zero equity"):
        advance_session(state, observation("fund", equity="1000", flows="2000"), reference())


def test_post_funding_return_uses_current_capital_and_preserves_peak():
    state = advance_session(start(), observation("fund", equity="2000", flows="2000"), reference())
    state = advance_session(state, observation("return", equity="2200", flows="2000"), reference())
    assert state.unit_index == state.lifetime_peak == Decimal("1.1")
    assert state.daily_pnl == 200 and state.e0 == 1000
