"""Fixed logistic/rule baselines with disjoint sigmoid calibration and JSON artifacts."""

import hashlib
import json
import math
import platform
import warnings
from dataclasses import asdict, dataclass
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from pydantic import Field
from sklearn.exceptions import ConvergenceWarning  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]

from prediction_bot.data_quality import _save
from prediction_bot.datasets import DatasetSnapshot, LabelPolicy
from prediction_bot.reference_data import FrozenModel
from prediction_bot.splits import SplitManifest, TrainingTransform, fit_training_transform


class BaselineConfig(FrozenModel):
    regularization_c: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    calibration_c: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    max_iterations: int = Field(default=1000, ge=1)
    threshold: float = Field(default=0.5, gt=0, lt=1)
    calibration_bins: int = Field(default=10, ge=2, le=100)
    seed: int = 0


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


@dataclass(frozen=True)
class BaselineArtifact:
    transform: TrainingTransform
    coefficients: tuple[float, ...]
    intercept: float
    calibration_slope: float
    calibration_intercept: float
    calibration_rows: tuple[int, ...]
    lineage: dict[str, str]

    def probabilities(
        self, matrix: tuple[tuple[float, ...], ...], *, calibrated: bool = True
    ) -> tuple[float, ...]:
        scaled = self.transform.transform(matrix)
        scores = tuple(
            self.intercept
            + math.fsum(
                value * weight for value, weight in zip(row, self.coefficients, strict=True)
            )
            for row in scaled
        )
        if not all(math.isfinite(score) for score in scores):
            raise ValueError("Prediction overflow")
        if calibrated:
            scores = tuple(
                self.calibration_slope * score + self.calibration_intercept for score in scores
            )
        if not all(math.isfinite(score) for score in scores):
            raise ValueError("Calibration overflow")
        return tuple(_sigmoid(score) for score in scores)

    def save(self, directory: Path) -> str:
        return _save(
            directory, json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode(), "json"
        )


def probability_metrics(
    targets: tuple[int, ...],
    probabilities: tuple[float, ...],
    *,
    threshold: float = 0.5,
    bins: int = 10,
) -> dict[str, object]:
    if (
        not targets
        or len(targets) != len(probabilities)
        or any(y not in (0, 1) for y in targets)
        or any(not math.isfinite(p) or not 0 <= p <= 1 for p in probabilities)
        or not 0 < threshold < 1
        or bins < 2
    ):
        raise ValueError("Invalid probability metrics inputs")
    predicted = tuple(int(p >= threshold) for p in probabilities)
    tp = sum(y == p == 1 for y, p in zip(targets, predicted, strict=True))
    tn = sum(y == p == 0 for y, p in zip(targets, predicted, strict=True))
    fp = sum(y == 0 and p == 1 for y, p in zip(targets, predicted, strict=True))
    fn = sum(y == 1 and p == 0 for y, p in zip(targets, predicted, strict=True))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    calibration = []
    for bucket in range(bins):
        indices = [i for i, p in enumerate(probabilities) if min(int(p * bins), bins - 1) == bucket]
        if indices:
            calibration.append(
                {
                    "lower": bucket / bins,
                    "upper": (bucket + 1) / bins,
                    "count": len(indices),
                    "mean_probability": math.fsum(probabilities[i] for i in indices) / len(indices),
                    "positive_fraction": sum(targets[i] for i in indices) / len(indices),
                }
            )
    return {
        "count": len(targets),
        "brier": math.fsum((p - y) ** 2 for y, p in zip(targets, probabilities, strict=True))
        / len(targets),
        "accuracy": (tp + tn) / len(targets),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "calibration": calibration,
    }


def train_fold(
    dataset: DatasetSnapshot,
    manifest: SplitManifest,
    fold_index: int,
    *,
    code_version: str,
    config: BaselineConfig | None = None,
) -> BaselineArtifact:
    if not code_version.strip():
        raise ValueError("An implementation Git revision is required")
    config = config or BaselineConfig()
    transform = fit_training_transform(dataset, manifest, fold_index)
    fold = manifest.folds[fold_index]
    matrix = dataset.feature_matrix()
    training_y = [dataset.rows[i].target.positive for i in fold.train]
    calibration_y = [dataset.rows[i].target.positive for i in fold.validation]
    if set(training_y) != {0, 1} or set(calibration_y) != {0, 1}:
        raise ValueError("Training and calibration partitions each require both classes")
    training_x = transform.transform(tuple(matrix[i] for i in fold.train))
    calibration_x = transform.transform(tuple(matrix[i] for i in fold.validation))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model = LogisticRegression(
            C=config.regularization_c,
            solver="lbfgs",
            random_state=config.seed,
            max_iter=config.max_iterations,
        ).fit(training_x, training_y)
        scores = model.decision_function(calibration_x).reshape(-1, 1)
        calibrator = LogisticRegression(
            C=config.calibration_c,
            solver="lbfgs",
            random_state=config.seed,
            max_iter=config.max_iterations,
        ).fit(scores, calibration_y)
    lineage = {
        "implementation": "logistic-sigmoid-v1",
        "code_version": code_version,
        "dataset_sha256": manifest.dataset_sha256,
        "split_sha256": hashlib.sha256(
            json.dumps(asdict(manifest), sort_keys=True).encode()
        ).hexdigest(),
        "config": config.model_dump_json(),
        "fold": str(fold_index),
        "python": platform.python_version(),
        "scikit_learn": version("scikit-learn"),
        "numpy": version("numpy"),
        "dataset_provenance": json.dumps(dataset.provenance, sort_keys=True),
    }
    return BaselineArtifact(
        transform,
        tuple(float(x) for x in model.coef_[0]),
        float(model.intercept_[0]),
        float(calibrator.coef_[0][0]),
        float(calibrator.intercept_[0]),
        fold.validation,
        lineage,
    )


def _comparisons(
    dataset: DatasetSnapshot,
    indices: tuple[int, ...],
    probabilities: tuple[float, ...],
    threshold: float,
) -> dict[str, object]:
    policy = LabelPolicy.model_validate_json(dataset.provenance["policy"])
    selected = dict(zip(indices, probabilities, strict=True))
    results: dict[str, object] = {}
    for instrument in sorted({dataset.rows[i].instrument_id for i in indices}):
        group = sorted(
            (i for i in indices if dataset.rows[i].instrument_id == instrument),
            key=lambda i: dataset.rows[i].signal_at,
        )
        equity = 1.0
        busy_until = None
        trades = 0
        for i in group:
            row = dataset.rows[i]
            if not math.isfinite(row.target.net_return) or row.target.net_return < -1:
                raise ValueError("Invalid test return")
            if selected[i] >= threshold and (
                busy_until is None or row.target.entry_at > busy_until
            ):
                equity *= 1 + row.target.net_return
                busy_until = row.target.exit_at
                trades += 1
        first = dataset.rows[group[0]].target
        last = max((dataset.rows[i].target for i in group), key=lambda target: target.exit_at)
        covered_until = first.exit_at
        continuous = True
        for i in group[1:]:
            target = dataset.rows[i].target
            if target.entry_at > covered_until:
                continuous = False
            covered_until = max(covered_until, target.exit_at)
        buy_hold = None
        if continuous:
            fee = policy.fee_bps_per_side / Decimal(10000)
            slip = policy.slippage_bps_per_side / Decimal(10000)
            buy_hold = float(
                Decimal(last.exit_close)
                * (1 - slip)
                * (1 - fee)
                / (Decimal(first.entry_open) * (1 + slip) * (1 + fee))
                - 1
            )
        results[instrument] = {
            "strategy_net_return": equity - 1,
            "nonoverlapping_trades": trades,
            "cash_net_return": 0.0,
            "buy_hold_net_return": buy_hold,
            "buy_hold_limitation": None if continuous else "unverified_gap_between_label_windows",
        }
    return results


def evaluate_fold(
    dataset: DatasetSnapshot,
    manifest: SplitManifest,
    fold_index: int,
    *,
    code_version: str,
    config: BaselineConfig | None = None,
) -> tuple[BaselineArtifact, dict[str, object]]:
    config = config or BaselineConfig()
    artifact = train_fold(dataset, manifest, fold_index, code_version=code_version, config=config)
    test = manifest.folds[fold_index].test
    matrix = dataset.feature_matrix()
    test_x = tuple(matrix[i] for i in test)
    targets = tuple(dataset.rows[i].target.positive for i in test)
    if "return" not in dataset.feature_schema:
        raise ValueError("Momentum baseline requires the return feature")
    momentum_column = dataset.feature_schema.index("return")
    predictions = {
        "logistic": artifact.probabilities(test_x, calibrated=False),
        "calibrated_logistic": artifact.probabilities(test_x),
        "momentum_rule": tuple(float(row[momentum_column] > 0) for row in test_x),
        "always_long": tuple(1.0 for _ in test),
        "cash": tuple(0.0 for _ in test),
    }
    report: dict[str, object] = {
        "fold": fold_index,
        "test_rows": test,
        "final_holdout_evaluated": False,
        "metrics": {
            name: probability_metrics(
                targets, values, threshold=config.threshold, bins=config.calibration_bins
            )
            for name, values in predictions.items()
        },
        "comparisons": {
            name: _comparisons(dataset, test, values, config.threshold)
            for name, values in predictions.items()
        },
        "probabilities": predictions,
        "lineage": artifact.lineage,
    }
    return artifact, report


def save_report(report: dict[str, object], directory: Path) -> str:
    return _save(directory, json.dumps(report, sort_keys=True, allow_nan=False).encode(), "json")
