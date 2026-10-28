# Baseline models and disjoint calibration

`evaluate_fold(dataset, split_manifest, fold_index, code_version=git_revision,
config=BaselineConfig())` returns a fitted JSON-compatible artifact and a test-fold
report. `train_fold(...)` fits without evaluating test outcomes. Inputs are the
versioned dataset and verified purged split manifest from the preceding modules.

The base model is scikit-learn logistic regression with an intercept, L2
regularization, LBFGS and an explicit seed. Scaling and coefficients are fitted
only on training rows. A separate regularized logistic model fits a sigmoid to
the base model's decision scores on validation rows. Base coefficients remain
frozen during calibration. Both partitions must contain both classes; missing
classes or convergence failure are errors, not reasons to silently fit on test
data. Configuration fixes regularization, iteration limit, threshold and bin count
before evaluation; this implementation does not optimize them on test results.

This uses the logistic estimator documented by
[scikit-learn](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html)
and follows its requirement for
[disjoint classifier and calibration fitting data](https://scikit-learn.org/stable/modules/calibration.html).
The calibrator is explicitly a regularized sigmoid fit, rather than a claim of
identical parameters to every Platt-scaling implementation.

Test-fold reports include uncalibrated logistic, calibrated logistic, a fixed
positive-return momentum rule, always-long and cash baselines. Metrics are Brier
score, accuracy, precision, recall, F1, confusion counts and equal-width
reliability bins with counts, predicted probabilities and observed positive
fractions. Zero-denominator precision/recall/F1 are defined as zero. Empty bins
are omitted. Brier score assesses prediction quality as well as calibration, so
it is reported alongside the bins; calibration need not improve every test fold.

Return comparisons are per instrument, using the dataset's explicit costs. Each
strategy holds at most one position per instrument; overlapping candidate labels
cannot create overlapping trades. Closed trades compound their net returns and
cash earns zero. Buy-and-hold pays entry costs once at the first test entry and
exit costs once at the final test exit. It is only reported when verified label
windows continuously cover that holding interval; otherwise an explicit gap
limitation is returned. These simplified comparisons are not a capital-constrained
portfolio backtest, and no cross-fold overlapping-return aggregation is provided.

Final holdout indices never enter model fitting, calibration or the reported
evaluation. Dataset hashes bind the complete artifact for integrity; hashing does
not use holdout outcomes for model selection. There is no automatic model
promotion or final-holdout scoring in this API. Keep the frozen experiment binding
and reserve final assessment for the later evaluation workflow.

`artifact.save(directory)` writes an immutable JSON file containing coefficients,
calibration parameters, training transform/rows, calibration rows, dataset hash,
split hash, full configuration, source Git revision, dependency versions and
dataset provenance. Prediction uses those numeric parameters without pickle or
executable serialized objects. `save_report(report, directory)` persists metrics
and test probabilities separately. Preserve the referenced dataset and manifest.

Tests verify hand-calculated metrics, deterministic fitting, disjoint calibration,
test/holdout parameter invariance, explicit single-class failures, artifact lineage
and non-overlapping return comparisons. They use synthetic data only; no real
model quality or expected trading return is asserted. Live execution stays disabled.
