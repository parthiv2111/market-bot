import math
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from prediction_bot.indicators import IndicatorConfig, KnownBar, features_at
from prediction_bot.providers import PriceBar, ProviderError
from prediction_bot.reference_data import ExchangeDay, ReferenceBundle

CONFIG = IndicatorConfig(
    sma_period=3,
    ema_period=3,
    rsi_period=3,
    macd_fast=2,
    macd_slow=3,
    macd_signal=2,
    atr_period=3,
    volatility_period=3,
    volume_period=3,
)


def instant(day, hour=10):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


@pytest.fixture
def reference():
    return ReferenceBundle(
        source="synthetic",
        published_at=instant(1),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 8),
        aliases=(),
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


def bars(closes=(10, 12, 11, 14, 13)):
    return tuple(
        KnownBar(
            PriceBar(
                "NSE",
                "SYNTH",
                date(2026, 1, i + 2),
                Decimal(c),
                Decimal(c + 1),
                Decimal(c - 1),
                Decimal(c),
                (i + 1) * 100,
            ),
            instant(i + 2),
        )
        for i, c in enumerate(closes)
    )


def calculate(reference, observations=None, day=6, **kwargs):
    return features_at(
        bars() if observations is None else observations,
        reference,
        symbol="SYNTH",
        session_date=date(2026, 1, day),
        as_of=kwargs.pop("as_of", instant(day)),
        config=CONFIG,
        **kwargs,
    )


def test_hand_calculated_fixture(reference):
    values = calculate(reference).values
    expected = {
        "return": -1 / 14,
        "sma": 38 / 3,
        "ema": 12.75,
        "rsi": 200 / 3,
        "macd": 0.25,
        "macd_signal": 0.25,
        "macd_histogram": 0,
        "atr": 8 / 3,
        "volume_sma": 400,
        "relative_volume": 5 / 3,
    }
    # Returns are -1/12, 3/11, -1/14; population mean is 109/2772.
    expected["volatility"] = math.sqrt(
        float(
            (Fraction(1, 144) + Fraction(9, 121) + Fraction(1, 196)) / 3 - Fraction(109, 2772) ** 2
        )
    )
    for name, value in expected.items():
        assert values[name].value == pytest.approx(value, abs=1e-12, rel=1e-12)
        assert values[name].invalid_reason is None


def test_seed_and_warmup_boundaries(reference):
    first = calculate(reference, day=2).values
    assert all(feature.value is None for feature in first.values())
    seeded = calculate(reference, day=4).values
    assert seeded["sma"].value == seeded["ema"].value == 11
    assert seeded["rsi"].invalid_reason == "insufficient_history"
    assert seeded["atr"].invalid_reason == "insufficient_history"
    assert seeded["macd"].value == 0
    assert seeded["macd_signal"].invalid_reason == "insufficient_history"
    wilder = calculate(reference, day=5).values
    assert wilder["rsi"].value == pytest.approx(250 / 3)
    assert wilder["atr"].value == 3


def test_future_changes_and_append_cannot_change_historical_features(reference):
    base = bars()
    future = KnownBar(
        replace(base[-1].bar, session_date=date(2026, 1, 7), close=Decimal("NaN")), instant(7)
    )
    original = calculate(reference, base[:3], day=4)
    assert calculate(reference, base + (future,), day=4) == original
    mutated = base[:3] + tuple(
        replace(item, bar=replace(item.bar, close=Decimal(-1))) for item in base[3:]
    )
    assert calculate(reference, mutated, day=4) == original


def test_unfinished_or_unknown_bar_is_explicitly_invalid(reference):
    assert (
        calculate(reference, as_of=instant(6, 9)).values["sma"].invalid_reason
        == "incomplete_session"
    )
    late = bars()[:-1] + (replace(bars()[-1], known_at=instant(7)),)
    assert calculate(reference, late).values["sma"].invalid_reason == "missing_bar"
    premature = (replace(bars()[0], known_at=instant(2, 8)),) + bars()[1:]
    assert calculate(reference, premature).values["sma"].invalid_reason == "invalid_knowledge_time"


def test_gap_restarts_warmups_instead_of_compressing_time(reference):
    with_gap = bars()[:2] + bars()[3:]
    assert calculate(reference, with_gap).values["ema"].invalid_reason == "insufficient_history"
    assert calculate(reference, with_gap).values["return"].value == pytest.approx(-1 / 14)


@pytest.mark.parametrize(
    "closes,expected",
    [((10, 11, 12, 13, 14), 100), ((14, 13, 12, 11, 10), 0), ((10, 10, 10, 10, 10), 50)],
)
def test_rsi_zero_loss_gain_and_flat_conventions(reference, closes, expected):
    assert calculate(reference, bars(closes)).values["rsi"].value == expected


def test_zero_volume_has_explicit_relative_volume_reason(reference):
    zero = tuple(replace(item, bar=replace(item.bar, volume=0)) for item in bars())
    values = calculate(reference, zero).values
    assert values["volume_sma"].value == 0
    assert values["relative_volume"].invalid_reason == "zero_volume_baseline"


def test_ordering_is_deterministic_and_vendor_adjustments_ignored(reference):
    source = bars()
    modified = tuple(
        replace(
            item,
            bar=replace(
                item.bar,
                adjusted_open=Decimal(1),
                adjusted_high=Decimal(1),
                adjusted_low=Decimal(1),
                adjusted_close=Decimal(1),
            ),
        )
        for item in source
    )
    assert calculate(reference, source[::-1]) == calculate(reference, source)
    assert calculate(reference, modified) == calculate(reference, source)


def test_duplicate_and_invalid_input_rejected(reference):
    with pytest.raises(ProviderError, match="duplicate"):
        calculate(reference, bars() + bars()[-1:])
    with pytest.raises(ProviderError, match="positive"):
        calculate(
            reference,
            bars()[:-1] + (replace(bars()[-1], bar=replace(bars()[-1].bar, close=Decimal(-1))),),
        )
    with pytest.raises(ValueError, match="timezone"):
        calculate(reference, as_of=datetime(2026, 1, 6))
    with pytest.raises(ValueError, match="MACD"):
        IndicatorConfig(macd_fast=26)


@pytest.mark.parametrize("price", ["1e-400", "1e400"])
def test_unrepresentable_prices_fail_explicitly(reference, price):
    extreme = tuple(
        replace(
            item,
            bar=replace(
                item.bar,
                open=Decimal(price),
                high=Decimal(price),
                low=Decimal(price),
                close=Decimal(price),
            ),
        )
        for item in bars()
    )
    assert calculate(reference, extreme).values["sma"].invalid_reason == "numeric_overflow"
