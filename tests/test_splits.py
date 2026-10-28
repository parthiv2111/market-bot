import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from prediction_bot.datasets import DatasetRow, DatasetSnapshot, Label
from prediction_bot.splits import SplitPolicy, fit_training_transform, walk_forward

START = datetime(2026, 1, 1, 10, tzinfo=UTC)


@pytest.fixture
def dataset():
    rows = tuple(
        DatasetRow(
            symbol,
            symbol,
            START + timedelta(days=d),
            (float(d), 7.0),
            Label(
                START + timedelta(days=d, hours=18),
                START + timedelta(days=d + 2),
                "100",
                "101",
                0.01,
                1,
            ),
        )
        for d in range(60)
        for symbol in ("A", "B")
    )
    return DatasetSnapshot(
        ("return", "sma"), ("net_return", "positive"), rows, (), {"source": "synthetic"}
    )


@pytest.fixture
def policy():
    return SplitPolicy(
        min_train_sessions=5,
        validation_sessions=6,
        test_sessions=4,
        step_sessions=4,
        holdout_start=START + timedelta(days=45),
        holdout_end=START + timedelta(days=61),
    )


def test_every_label_partition_is_chronological_and_nonoverlapping(dataset, policy):
    manifest = walk_forward(dataset, policy)
    assert len(manifest.folds) > 1
    for fold in manifest.folds:
        for left, right in (
            (fold.train, fold.validation),
            (fold.train, fold.test),
            (fold.validation, fold.test),
        ):
            assert max(dataset.rows[i].target.exit_at for i in left) < min(
                dataset.rows[i].signal_at for i in right
            )
        assert max(dataset.rows[i].target.exit_at for i in fold.test) < policy.holdout_start
        assert not set(manifest.holdout) & set(fold.train + fold.validation + fold.test)
        assert len({dataset.rows[i].signal_at for i in fold.train}) >= policy.min_train_sessions
        assert fold.purged
        role_by_time = {}
        for role, indices in (
            ("train", fold.train),
            ("validation", fold.validation),
            ("test", fold.test),
        ):
            for i in indices:
                instant = dataset.rows[i].signal_at
                assert role_by_time.setdefault(instant, role) == role


def test_equal_boundary_and_long_label_intervals_are_purged(dataset, policy):
    original = walk_forward(dataset, policy)
    fold = original.folds[0]
    boundary = min(dataset.rows[i].signal_at for i in fold.validation)
    modified = list(dataset.rows)
    modified[0] = replace(modified[0], target=replace(modified[0].target, exit_at=boundary))
    result = walk_forward(replace(dataset, rows=tuple(modified)), policy)
    assert 0 not in result.folds[0].train
    assert 0 in result.folds[0].purged
    modified[0] = replace(
        modified[0], target=replace(modified[0].target, exit_at=policy.holdout_start)
    )
    result = walk_forward(replace(dataset, rows=tuple(modified)), policy)
    assert 0 in result.excluded


def test_changing_test_or_holdout_data_cannot_fit_training_transform(dataset, policy):
    manifest = walk_forward(dataset, policy)
    before = fit_training_transform(dataset, manifest, 0)
    changed_indices = set(manifest.folds[0].test + manifest.folds[0].validation + manifest.holdout)
    changed = replace(
        dataset,
        rows=tuple(
            replace(
                row,
                feature_values=(999999.0, -999999.0),
                target=replace(row.target, net_return=-0.8, positive=0),
            )
            if i in changed_indices
            else row
            for i, row in enumerate(dataset.rows)
        ),
    )
    after = fit_training_transform(changed, walk_forward(changed, policy), 0)
    assert after == before
    assert before.scale[1] == 1
    transformed = before.transform(tuple(dataset.feature_matrix()[i] for i in before.training_rows))
    assert sum(row[0] for row in transformed) == pytest.approx(0, abs=1e-12)
    assert all(row[1] == 0 for row in transformed)


def test_manifest_reproducible_and_final_holdout_frozen(dataset, policy, tmp_path):
    first = walk_forward(dataset, policy)
    assert first == walk_forward(dataset, policy)
    version = first.save(tmp_path)
    assert walk_forward(dataset, policy).save(tmp_path) == version
    binding = json.loads((tmp_path / "final-holdout.json").read_bytes())
    assert binding["dataset_sha256"] == first.dataset_sha256
    assert tuple(binding["rows"]) == first.holdout
    shifted = policy.model_copy(update={"holdout_start": policy.holdout_start + timedelta(days=1)})
    with pytest.raises(ValueError, match="frozen"):
        walk_forward(dataset, shifted).save(tmp_path)


def test_embargo_requires_rationale_and_adds_real_calendar_time_gap(dataset, policy):
    with pytest.raises(ValueError, match="rationale"):
        SplitPolicy(**{**policy.model_dump(), "embargo_days": 1})
    extra = SplitPolicy(
        **{
            **policy.model_dump(),
            "embargo_days": 1,
            "embargo_rationale": "Synthetic one-day data publication buffer",
        }
    )
    manifest = walk_forward(dataset, extra)
    for fold in manifest.folds:
        assert max(dataset.rows[i].target.exit_at for i in fold.train) < min(
            dataset.rows[i].signal_at for i in fold.validation
        ) - timedelta(days=1)


def test_tampered_indices_and_different_dataset_are_rejected(dataset, policy):
    manifest = walk_forward(dataset, policy)
    bad = replace(
        manifest, folds=(replace(manifest.folds[0], train=manifest.holdout), *manifest.folds[1:])
    )
    with pytest.raises(ValueError, match="integrity"):
        fit_training_transform(dataset, bad, 0)
    with pytest.raises(ValueError, match="does not match"):
        fit_training_transform(replace(dataset, provenance={"changed": "yes"}), manifest, 0)


def test_bad_timing_duplicates_and_insufficient_partitions_fail(dataset, policy):
    with pytest.raises(ValueError, match="Duplicate"):
        walk_forward(replace(dataset, rows=dataset.rows + dataset.rows[:1]), policy)
    bad = replace(dataset.rows[0], target=replace(dataset.rows[0].target, entry_at=START))
    with pytest.raises(ValueError, match="timing"):
        walk_forward(replace(dataset, rows=(bad, *dataset.rows[1:])), policy)
    with pytest.raises(ValueError, match="Insufficient"):
        walk_forward(dataset, policy.model_copy(update={"validation_sessions": 2}))
    with pytest.raises(ValueError, match="holdout"):
        walk_forward(replace(dataset, rows=dataset.rows[:20]), policy)


def test_transform_rejects_wrong_width_and_nonfinite_values(dataset, policy):
    transform = fit_training_transform(dataset, walk_forward(dataset, policy), 0)
    with pytest.raises(ValueError, match="schema"):
        transform.transform(((1.0,),))
    with pytest.raises(ValueError, match="finite"):
        transform.transform(((float("nan"), 2.0),))
