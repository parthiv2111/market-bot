"""Chronological folds with actual label-interval purging and train-only scaling."""

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import pstdev

from pydantic import Field, model_validator

from prediction_bot.data_quality import _aware, _save
from prediction_bot.datasets import DatasetSnapshot
from prediction_bot.reference_data import FrozenModel


class SplitPolicy(FrozenModel):
    min_train_sessions: int = Field(ge=1)
    validation_sessions: int = Field(ge=1)
    test_sessions: int = Field(ge=1)
    step_sessions: int = Field(ge=1)
    holdout_start: datetime
    holdout_end: datetime
    embargo_days: int = Field(default=0, ge=0)
    embargo_rationale: str = ""

    @model_validator(mode="after")
    def validate_policy(self) -> "SplitPolicy":
        _aware(self.holdout_start)
        _aware(self.holdout_end)
        if self.holdout_end <= self.holdout_start:
            raise ValueError("Holdout interval must be nonempty")
        if self.embargo_days and not self.embargo_rationale.strip():
            raise ValueError("An embargo requires an explicit rationale")
        return self


def dataset_hash(dataset: DatasetSnapshot) -> str:
    return hashlib.sha256(
        json.dumps(asdict(dataset), sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Fold:
    train: tuple[int, ...]
    validation: tuple[int, ...]
    test: tuple[int, ...]
    purged: tuple[int, ...]


@dataclass(frozen=True)
class SplitManifest:
    dataset_sha256: str
    policy: str
    folds: tuple[Fold, ...]
    holdout: tuple[int, ...]
    excluded: tuple[int, ...]
    implementation: str = "purged-walk-forward-v1"

    def save(self, directory: Path) -> str:
        policy = SplitPolicy.model_validate_json(self.policy)
        binding = json.dumps(
            {
                "dataset_sha256": self.dataset_sha256,
                "rows": self.holdout,
                "start": policy.holdout_start.isoformat(),
                "end": policy.holdout_end.isoformat(),
            },
            sort_keys=True,
        ).encode()
        version = _save(directory / "holdout-bindings", binding, "json")
        target = directory / "final-holdout.json"
        try:
            os.link(directory / "holdout-bindings" / f"{version}.json", target)
        except FileExistsError:
            if target.read_bytes() != binding:
                raise ValueError(
                    "Final holdout is frozen; use a separate declared experiment"
                ) from None
        return _save(
            directory / "manifests", json.dumps(asdict(self), sort_keys=True).encode(), "json"
        )


def walk_forward(dataset: DatasetSnapshot, policy: SplitPolicy) -> SplitManifest:
    if not dataset.feature_schema or len(set(dataset.feature_schema)) != len(
        dataset.feature_schema
    ):
        raise ValueError("Require a distinct nonempty feature schema")
    dataset.feature_matrix()  # Enforce indicator-only schema and row widths.
    seen = set()
    for row in dataset.rows:
        for timestamp in (row.signal_at, row.target.entry_at, row.target.exit_at):
            _aware(timestamp)
        if not row.signal_at < row.target.entry_at <= row.target.exit_at:
            raise ValueError("Invalid signal/label timing")
        key = (row.instrument_id, row.signal_at)
        if key in seen:
            raise ValueError("Duplicate instrument/signal row")
        seen.add(key)
    order = sorted(
        range(len(dataset.rows)),
        key=lambda i: (dataset.rows[i].signal_at, dataset.rows[i].instrument_id),
    )
    holdout = tuple(
        i
        for i in order
        if policy.holdout_start <= dataset.rows[i].signal_at < policy.holdout_end
        and dataset.rows[i].target.exit_at <= policy.holdout_end
    )
    if not holdout:
        raise ValueError("No complete final holdout rows")
    embargo = timedelta(days=policy.embargo_days)
    candidates = [
        i
        for i in order
        if dataset.rows[i].signal_at < policy.holdout_start
        and dataset.rows[i].target.exit_at < policy.holdout_start - embargo
    ]
    excluded = tuple(i for i in order if i not in candidates and i not in holdout)
    sessions = sorted({dataset.rows[i].signal_at for i in candidates})
    folds = []
    width = policy.validation_sessions + policy.test_sessions
    for anchor in range(policy.min_train_sessions, len(sessions) - width + 1, policy.step_sessions):
        validation_start = sessions[anchor]
        test_start = sessions[anchor + policy.validation_sessions]
        test_end = sessions[anchor + width - 1]
        train_candidates = [i for i in candidates if dataset.rows[i].signal_at < validation_start]
        validation_candidates = [
            i for i in candidates if validation_start <= dataset.rows[i].signal_at < test_start
        ]
        train = tuple(
            i
            for i in train_candidates
            if dataset.rows[i].target.exit_at < validation_start - embargo
        )
        validation = tuple(
            i
            for i in validation_candidates
            if dataset.rows[i].target.exit_at < test_start - embargo
        )
        test = tuple(i for i in candidates if test_start <= dataset.rows[i].signal_at <= test_end)
        if (
            len({dataset.rows[i].signal_at for i in train}) < policy.min_train_sessions
            or not validation
            or not test
        ):
            continue
        purged = tuple(
            i
            for i in train_candidates + validation_candidates
            if i not in train and i not in validation
        )
        folds.append(Fold(train, validation, test, purged))
    if not folds:
        raise ValueError("Insufficient complete sessions after purging")
    return SplitManifest(
        dataset_hash(dataset), policy.model_dump_json(), tuple(folds), holdout, excluded
    )


@dataclass(frozen=True)
class TrainingTransform:
    feature_schema: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    training_rows: tuple[int, ...]
    training_features_sha256: str

    def save(self, directory: Path) -> str:
        return _save(
            directory, json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode(), "json"
        )

    def transform(self, values: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
        if any(
            len(row) != len(self.feature_schema) or not all(math.isfinite(value) for value in row)
            for row in values
        ):
            raise ValueError("Require finite values matching the feature schema")
        output = tuple(
            tuple(
                (value - mean) / scale
                for value, mean, scale in zip(row, self.mean, self.scale, strict=True)
            )
            for row in values
        )
        if not all(math.isfinite(value) for row in output for value in row):
            raise ValueError("Transform overflow")
        return output


def fit_training_transform(
    dataset: DatasetSnapshot, manifest: SplitManifest, fold_index: int
) -> TrainingTransform:
    if dataset_hash(dataset) != manifest.dataset_sha256:
        raise ValueError("Dataset does not match split manifest")
    # Verify partitions against the recorded policy rather than trusting supplied row indices.
    if manifest != walk_forward(dataset, SplitPolicy.model_validate_json(manifest.policy)):
        raise ValueError("Split manifest integrity failure")
    if not 0 <= fold_index < len(manifest.folds):
        raise ValueError("Unknown fold")
    indices = manifest.folds[fold_index].train
    matrix = dataset.feature_matrix()
    columns = tuple(zip(*(matrix[i] for i in indices), strict=True))
    if not all(math.isfinite(value) for column in columns for value in column):
        raise ValueError("Training features must be finite")
    means = tuple(math.fsum(value / len(column) for value in column) for column in columns)
    scales = tuple(pstdev(column) or 1.0 for column in columns)
    if not all(math.isfinite(value) for value in means + scales):
        raise ValueError("Training transform overflow")
    training_hash = hashlib.sha256(
        json.dumps(
            {"schema": dataset.feature_schema, "rows": [(i, matrix[i]) for i in indices]},
            sort_keys=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return TrainingTransform(dataset.feature_schema, means, scales, indices, training_hash)
