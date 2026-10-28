import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from prediction_bot.data_quality import CorporateAction
from prediction_bot.datasets import LabelPolicy, build_dataset
from prediction_bot.indicators import KnownBar
from prediction_bot.providers import PriceBar
from prediction_bot.reference_data import ExchangeDay, InstrumentAlias, ReferenceBundle
from prediction_bot.universe import Constituent, ConstituentSnapshot, Universe


def instant(day, hour=10):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


@pytest.fixture
def inputs():
    days = (2, 3, 5, 6, 8, 9, 10, 11, 12)
    reference = ReferenceBundle(
        source="synthetic",
        published_at=instant(1),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 12),
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
            if d in days
            else ExchangeDay(
                session_date=date(2026, 1, d), status="closed", reason="synthetic holiday"
            )
            for d in range(2, 13)
        ),
    )
    universe = Universe(
        name="synthetic",
        snapshots=(
            ConstituentSnapshot(
                valid_from=date(2026, 1, 2),
                valid_to=date(2026, 1, 13),
                known_at=instant(1),
                source="synthetic",
                members=(Constituent(instrument_id="stable", symbol="SYNTH"),),
            ),
        ),
    )
    prices = (100, 110, 120, 125, 130, 140, 150, 160, 170)
    bars = tuple(
        KnownBar(
            PriceBar(
                "NSE",
                "SYNTH",
                date(2026, 1, d),
                Decimal(p),
                Decimal(p + 1),
                Decimal(p - 1),
                Decimal(p),
                1000,
            ),
            instant(d),
        )
        for d, p in zip(days, prices, strict=True)
    )
    return bars, universe, reference


def policy(**changes):
    values = dict(
        fee_bps_per_side=Decimal(10),
        slippage_bps_per_side=Decimal(20),
        action_coverage_start=date(2026, 1, 2),
        action_coverage_end=date(2026, 1, 12),
        action_source="synthetic no-action ledger",
    )
    values.update(changes)
    return LabelPolicy(**values)


def build(inputs, **kwargs):
    bars, universe, reference = inputs
    values = dict(
        start=date(2026, 1, 3),
        end=date(2026, 1, 3),
        as_of=instant(12),
        policy=policy(),
        feature_schema=("return",),
    )
    values.update(kwargs)
    actions = values.pop("actions", ())
    return build_dataset(bars, universe, reference, actions, **values)


def test_hand_checked_next_open_and_fifth_session_close(inputs):
    result = build(inputs)
    (row,) = result.rows
    assert row.signal_at == instant(3)
    assert row.target.entry_at == instant(5, 4)  # Holiday on Jan 4.
    assert row.target.exit_at == instant(10)  # Sessions 5, 6, 8, 9, 10.
    assert row.target.entry_open == "120"
    assert row.target.exit_close == "150"
    expected = float(
        Decimal(150)
        * Decimal(".998")
        * Decimal(".999")
        / (Decimal(120) * Decimal("1.002") * Decimal("1.001"))
        - 1
    )
    assert row.target.net_return == pytest.approx(expected, abs=1e-12)
    assert row.target.positive == 1
    assert row.feature_values == pytest.approx((0.1,))
    assert result.excluded == ()


def test_incomplete_end_rows_excluded(inputs):
    result = build(inputs, end=date(2026, 1, 12))
    assert len(result.rows) == 3
    assert all(row.target.exit_at <= instant(12) for row in result.rows)
    assert any(row.session_date == date(2026, 1, 12) for row in result.excluded)
    missing = build(inputs, as_of=instant(9))
    assert not missing.rows
    assert missing.excluded[0].reason == "incomplete_five_session_horizon"


def test_future_prices_change_labels_but_never_feature_matrix(inputs):
    before = build(inputs)
    bars, universe, reference = inputs
    changed = tuple(
        replace(
            item,
            bar=replace(
                item.bar,
                open=item.bar.open * 2,
                high=item.bar.high * 2,
                low=item.bar.low * 2,
                close=item.bar.close * 2,
            ),
        )
        if item.bar.session_date == date(2026, 1, 10)
        else item
        for item in bars
    )
    after = build((changed, universe, reference))
    assert after.feature_matrix() == before.feature_matrix()
    assert after.rows[0].target.net_return != before.rows[0].target.net_return
    assert (
        replace(
            before, rows=(replace(before.rows[0], target=after.rows[0].target),)
        ).feature_matrix()
        == before.feature_matrix()
    )


@pytest.mark.parametrize(
    "schema", [("net_return",), ("positive",), ("entry_open",), ("return", "return"), ()]
)
def test_targets_and_invalid_schemas_cannot_enter_features(inputs, schema):
    with pytest.raises(ValueError, match="feature columns"):
        build(inputs, feature_schema=schema)


def test_late_signal_bar_cannot_enter_features(inputs):
    bars, universe, reference = inputs
    late = tuple(
        replace(item, known_at=instant(5)) if item.bar.session_date == date(2026, 1, 3) else item
        for item in bars
    )
    result = build((late, universe, reference))
    assert not result.rows
    assert result.excluded[0].reason == "invalid_features"


def test_missing_middle_session_is_not_hidden_by_complete_endpoints(inputs):
    bars, universe, reference = inputs
    result = build(
        (tuple(b for b in bars if b.bar.session_date != date(2026, 1, 8)), universe, reference)
    )
    assert not result.rows
    assert result.excluded[0].reason == "incomplete_five_session_horizon"


def test_action_windows_and_unknown_action_coverage_are_excluded(inputs):
    action = CorporateAction(
        action_id="split",
        symbol="SYNTH",
        kind="split",
        effective_at=instant(8, 4),
        known_at=instant(6),
        source="synthetic",
        price_factor=Decimal(".5"),
        new_shares_per_old=Decimal(2),
    )
    result = build(inputs, actions=(action,))
    assert not result.rows
    assert result.excluded[0].reason == "corporate_action_in_holding_window"
    result = build(inputs, policy=policy(action_coverage_end=date(2026, 1, 9)))
    assert result.excluded[0].reason == "corporate_action_coverage_unavailable"


def test_snapshots_have_schemas_hashes_and_deterministic_ids(inputs, tmp_path):
    result = build(inputs)
    version = result.save(tmp_path)
    assert build(inputs).save(tmp_path) == version
    assert build(inputs, policy=policy(fee_bps_per_side=Decimal(11))).save(tmp_path) != version
    payload = json.loads((tmp_path / f"{version}.json").read_bytes())
    assert payload["feature_schema"] == ["return"]
    assert payload["label_schema"] == ["net_return", "positive"]
    assert len(payload["provenance"]["observations_sha256"]) == 64
    assert "target" in payload["rows"][0]
    with pytest.raises(ValueError, match="target column"):
        replace(result, feature_schema=("net_return",)).feature_matrix()


def test_invalid_inputs_and_intraday_policy_rejected(inputs):
    with pytest.raises(ValueError):
        policy(implementation="intraday")
    with pytest.raises(ValueError):
        policy(fee_bps_per_side=-1)
    with pytest.raises(ValueError):
        LabelPolicy(action_source="missing costs")
    bars, universe, reference = inputs
    with pytest.raises(ValueError, match="Duplicate"):
        build((bars + bars[:1], universe, reference))
    with pytest.raises(ValueError, match="completed daily"):
        build(((replace(bars[0], known_at=instant(2, 3)),) + bars[1:], universe, reference))


def test_costs_can_change_positive_target_and_zero_volume_cannot_fill(inputs):
    result = build(inputs, policy=policy(fee_bps_per_side=Decimal(2000), slippage_bps_per_side=0))
    assert result.rows[0].target.net_return == pytest.approx(-1 / 6)
    assert result.rows[0].target.positive == 0
    bars, universe, reference = inputs
    stopped = tuple(
        replace(item, bar=replace(item.bar, volume=0))
        if item.bar.session_date == date(2026, 1, 5)
        else item
        for item in bars
    )
    result = build((stopped, universe, reference))
    assert not result.rows
    assert result.excluded[0].reason == "nontrading_horizon_observation"
