import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from prediction_bot.indicators import Feature, IndicatorConfig, KnownBar, features_at
from prediction_bot.providers import PriceBar
from prediction_bot.reference_data import ExchangeDay, ReferenceBundle
from prediction_bot.regimes import RegimeRules, classify_regime, regime_at

CONFIG = IndicatorConfig(ema_period=2, sma_period=3, volatility_period=2)
RULES = RegimeRules(
    indicators=CONFIG, trend_threshold=0.01, high_volatility_threshold=0.5, entry_regimes=("bull",)
)
DAY = date(2026, 1, 6)
CUTOFF = datetime(2026, 1, 6, 10, tzinfo=UTC)


@pytest.fixture
def reference():
    return ReferenceBundle(
        source="synthetic",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 7),
        aliases=(),
        days=tuple(
            ExchangeDay(
                session_date=date(2026, 1, d),
                status="open",
                reason="synthetic",
                opens_at=datetime(2026, 1, d, 4, tzinfo=UTC),
                closes_at=datetime(2026, 1, d, 10, tzinfo=UTC),
            )
            for d in range(2, 8)
        ),
    )


def series(closes=(10, 11, 12, 13, 14), symbol="INDEX"):
    return tuple(
        KnownBar(
            PriceBar(
                "NSE",
                symbol,
                date(2026, 1, i + 2),
                Decimal(c),
                Decimal(c + 1),
                Decimal(c - 1),
                Decimal(c),
                100,
            ),
            datetime(2026, 1, i + 2, 10, tzinfo=UTC),
        )
        for i, c in enumerate(closes)
    )


def evaluate(reference, closes=(10, 11, 12, 13, 14), **kwargs):
    return regime_at(
        series(closes) + series(closes, "EQUITY"),
        reference,
        index_symbol="INDEX",
        equity_symbol="EQUITY",
        session_date=DAY,
        as_of=CUTOFF,
        rules=kwargs.pop("rules", RULES),
        **kwargs,
    )


@pytest.mark.parametrize(
    "closes,expected",
    [
        ((10, 11, 12, 13, 14), "bull"),
        ((14, 13, 12, 11, 10), "bear"),
        ((10, 10, 10, 10, 10), "range"),
        ((10, 30, 10, 30, 10), "high_volatility"),
    ],
)
def test_synthetic_regimes(reference, closes, expected):
    decision = evaluate(reference, closes)
    assert decision.regime == expected
    assert decision.entry_eligible == (expected == "bull")
    assert decision.reasons == ()
    assert decision.rule_version == RULES.version
    assert json.loads(decision.rule_payload)["trend_threshold"] == 0.01
    assert decision.inputs["index.feature_version"] == "indicators-v1"


def test_default_rules_do_not_authorize_entries(reference):
    rules = RegimeRules(indicators=CONFIG)
    assert not evaluate(reference, rules=rules).entry_eligible


def test_missing_or_unknown_regime_always_blocks(reference):
    rules = RULES.model_copy(update={"entry_regimes": ("bull", "bear", "range", "high_volatility")})
    missing = classify_regime(None, None, session_date=DAY, as_of=CUTOFF, rules=rules)
    assert missing.regime == "unknown"
    assert not missing.entry_eligible
    assert missing.reasons == ("index:missing_features", "equity:missing_features")
    insufficient = regime_at(
        series()[-1:] + series(symbol="EQUITY")[-1:],
        reference,
        index_symbol="INDEX",
        equity_symbol="EQUITY",
        session_date=DAY,
        as_of=CUTOFF,
        rules=rules,
    )
    assert insufficient.regime == "unknown"
    assert not insufficient.entry_eligible
    assert any("insufficient_history" in reason for reason in insufficient.reasons)


def test_future_mutation_cannot_change_regime(reference):
    source = series() + series(symbol="EQUITY")
    original = regime_at(
        source,
        reference,
        index_symbol="INDEX",
        equity_symbol="EQUITY",
        session_date=DAY,
        as_of=CUTOFF,
        rules=RULES,
    )
    future = KnownBar(
        replace(source[-1].bar, session_date=date(2026, 1, 7), close=Decimal(-100)),
        datetime(2026, 1, 7, tzinfo=UTC),
    )
    assert (
        regime_at(
            source + (future,),
            reference,
            index_symbol="INDEX",
            equity_symbol="EQUITY",
            session_date=DAY,
            as_of=CUTOFF,
            rules=RULES,
        )
        == original
    )


def test_disagreeing_index_and_equity_are_range(reference):
    source = series() + series((14, 13, 12, 11, 10), "EQUITY")
    result = regime_at(
        source,
        reference,
        index_symbol="INDEX",
        equity_symbol="EQUITY",
        session_date=DAY,
        as_of=CUTOFF,
        rules=RULES,
    )
    assert result.regime == "range"
    assert not result.entry_eligible


def test_stale_mismatched_and_nonfinite_features_block(reference):
    index = features_at(
        series(), reference, symbol="INDEX", session_date=DAY, as_of=CUTOFF, config=CONFIG
    )
    equity = replace(index, symbol="EQUITY")
    for invalid in (
        replace(index, session_date=date(2026, 1, 5)),
        replace(index, as_of=datetime(2026, 1, 7, tzinfo=UTC)),
        replace(index, config=IndicatorConfig()),
        replace(index, values={**index.values, "sma": Feature(float("nan"))}),
    ):
        result = classify_regime(invalid, equity, session_date=DAY, as_of=CUTOFF, rules=RULES)
        assert result.regime == "unknown"
        assert not result.entry_eligible


def test_rule_changes_version_and_saved_decision(reference, tmp_path):
    original = evaluate(reference)
    changed = evaluate(reference, rules=RULES.model_copy(update={"trend_threshold": 0.02}))
    assert changed.rule_version != original.rule_version
    assert original.save(tmp_path) == evaluate(reference).save(tmp_path)
    payload = json.loads((tmp_path / f"{original.save(tmp_path)}.json").read_bytes())
    assert payload["inputs"]["equity.symbol"] == "EQUITY"
    assert payload["rule_version"] == original.rule_version


def test_invalid_rules_and_duplicate_symbols_rejected(reference):
    with pytest.raises(ValueError):
        RegimeRules(entry_regimes=("unknown",))
    with pytest.raises(ValueError):
        RegimeRules(high_volatility_threshold=float("nan"))
    with pytest.raises(ValueError, match="shorter"):
        RegimeRules(indicators=IndicatorConfig())
    with pytest.raises(ValueError, match="distinct"):
        regime_at(
            series(),
            reference,
            index_symbol="INDEX",
            equity_symbol="INDEX",
            session_date=DAY,
            as_of=CUTOFF,
        )
