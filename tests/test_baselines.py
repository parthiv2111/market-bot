import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from prediction_bot.baselines import evaluate_fold, probability_metrics, save_report, train_fold
from prediction_bot.datasets import DatasetRow, DatasetSnapshot, Label, LabelPolicy
from prediction_bot.splits import SplitPolicy, walk_forward

START = datetime(2026, 1, 1, 10, tzinfo=UTC)


@pytest.fixture
def inputs():
    rows = tuple(
        DatasetRow(
            "SYNTH",
            "SYNTH",
            START + timedelta(days=i),
            ((1.0 if i % 2 else -1.0) * 0.01, float(i % 5)),
            Label(
                START + timedelta(days=i, hours=18),
                START + timedelta(days=i + 2),
                "100",
                "101" if i % 2 else "99",
                0.01 if i % 2 else -0.01,
                i % 2,
            ),
        )
        for i in range(100)
    )
    cost = LabelPolicy(
        fee_bps_per_side=0,
        slippage_bps_per_side=0,
        action_coverage_start=date(2026, 1, 1),
        action_coverage_end=date(2027, 1, 1),
        action_source="synthetic",
    )
    dataset = DatasetSnapshot(
        ("return", "sma"), ("net_return", "positive"), rows, (), {"policy": cost.model_dump_json()}
    )
    policy = SplitPolicy(
        min_train_sessions=20,
        validation_sessions=12,
        test_sessions=10,
        step_sessions=10,
        holdout_start=START + timedelta(days=80),
        holdout_end=START + timedelta(days=102),
    )
    return dataset, policy


def test_hand_checked_probability_metrics():
    metrics = probability_metrics((0, 1), (0.2, 0.8), bins=2)
    assert metrics["brier"] == pytest.approx(0.04)
    assert metrics["accuracy"] == metrics["precision"] == metrics["recall"] == metrics["f1"] == 1
    assert metrics["calibration"] == [
        {"lower": 0, "upper": 0.5, "count": 1, "mean_probability": 0.2, "positive_fraction": 0},
        {"lower": 0.5, "upper": 1, "count": 1, "mean_probability": 0.8, "positive_fraction": 1},
    ]


def test_baselines_report_metrics_comparisons_and_lineage(inputs, tmp_path):
    dataset, policy = inputs
    manifest = walk_forward(dataset, policy)
    artifact, report = evaluate_fold(dataset, manifest, 0, code_version="synthetic-revision")
    assert set(report["metrics"]) == {
        "logistic",
        "calibrated_logistic",
        "momentum_rule",
        "always_long",
        "cash",
    }
    assert report["final_holdout_evaluated"] is False
    assert artifact.transform.training_rows == manifest.folds[0].train
    assert artifact.calibration_rows == manifest.folds[0].validation
    assert not set(manifest.holdout) & set(
        artifact.transform.training_rows + artifact.calibration_rows + report["test_rows"]
    )
    for name, metrics in report["metrics"].items():
        assert 0 <= metrics["brier"] <= 1
        assert sum(bucket["count"] for bucket in metrics["calibration"]) == len(report["test_rows"])
        comparison = report["comparisons"][name]["SYNTH"]
        assert comparison["cash_net_return"] == 0
        assert comparison["buy_hold_net_return"] is not None
        assert comparison["nonoverlapping_trades"] < len(report["test_rows"])
    assert report["comparisons"]["cash"]["SYNTH"]["strategy_net_return"] == 0
    version = artifact.save(tmp_path / "models")
    payload = json.loads((tmp_path / "models" / f"{version}.json").read_bytes())
    assert payload["lineage"]["dataset_sha256"] == manifest.dataset_sha256
    assert payload["lineage"]["code_version"] == "synthetic-revision"
    assert save_report(report, tmp_path / "reports") == save_report(report, tmp_path / "reports")


def test_test_and_holdout_changes_cannot_change_fitted_parameters(inputs):
    dataset, policy = inputs
    manifest = walk_forward(dataset, policy)
    before = train_fold(dataset, manifest, 0, code_version="fixture")
    forbidden = set(manifest.folds[0].test + manifest.holdout)
    changed = replace(
        dataset,
        rows=tuple(
            replace(
                row,
                feature_values=(9000.0, -9000.0),
                target=replace(row.target, positive=1 - row.target.positive),
            )
            if i in forbidden
            else row
            for i, row in enumerate(dataset.rows)
        ),
    )
    after = train_fold(changed, walk_forward(changed, policy), 0, code_version="fixture")
    assert after.transform == before.transform
    assert after.coefficients == before.coefficients
    assert after.intercept == before.intercept
    assert after.calibration_slope == before.calibration_slope
    assert after.calibration_intercept == before.calibration_intercept


def test_only_validation_changes_calibrator_not_base_model(inputs):
    dataset, policy = inputs
    manifest = walk_forward(dataset, policy)
    before = train_fold(dataset, manifest, 0, code_version="fixture")
    validation = set(manifest.folds[0].validation)
    changed = replace(
        dataset,
        rows=tuple(
            replace(row, target=replace(row.target, positive=1 - row.target.positive))
            if i in validation
            else row
            for i, row in enumerate(dataset.rows)
        ),
    )
    after = train_fold(changed, walk_forward(changed, policy), 0, code_version="fixture")
    assert before.coefficients == after.coefficients
    assert before.calibration_slope != after.calibration_slope


def test_repeat_fit_and_predictions_are_reproducible(inputs):
    dataset, policy = inputs
    manifest = walk_forward(dataset, policy)
    left = train_fold(dataset, manifest, 0, code_version="fixture")
    right = train_fold(dataset, manifest, 0, code_version="fixture")
    assert left == right
    matrix = dataset.feature_matrix()
    assert left.probabilities(matrix) == right.probabilities(matrix)


def test_single_class_calibration_and_missing_lineage_fail(inputs):
    dataset, policy = inputs
    manifest = walk_forward(dataset, policy)
    selected = set(manifest.folds[0].validation)
    changed = replace(
        dataset,
        rows=tuple(
            replace(row, target=replace(row.target, positive=1)) if i in selected else row
            for i, row in enumerate(dataset.rows)
        ),
    )
    with pytest.raises(ValueError, match="both classes"):
        train_fold(changed, walk_forward(changed, policy), 0, code_version="fixture")
    with pytest.raises(ValueError, match="revision"):
        train_fold(dataset, manifest, 0, code_version="")


@pytest.mark.parametrize(
    "targets,probabilities", [((), ()), ((0,), (float("nan"),)), ((2,), (0.5,)), ((0,), (1.1,))]
)
def test_invalid_metrics_are_rejected(targets, probabilities):
    with pytest.raises(ValueError):
        probability_metrics(targets, probabilities)
