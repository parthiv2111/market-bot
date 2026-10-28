# Purged chronological walk-forward validation

`walk_forward(dataset, SplitPolicy(...))` creates expanding training folds followed
by validation and test windows. Counts refer to distinct signal timestamps in the
supplied dataset, not row counts or inferred weekdays. All instruments sharing a
signal timestamp stay in the same candidate partition. Row indices point into
the original immutable dataset; shuffled input is ordered by signal time and
instrument identity before partitioning.

Supply `min_train_sessions`, `validation_sessions`, `test_sessions`,
`step_sessions`, and timezone-aware `holdout_start` / `holdout_end`. The final
holdout is selected by signal timestamps in the half-open interval, and every
included label must also finish by its end. No holdout row enters a development
fold. Pre-holdout rows whose labels reach the holdout boundary are excluded.

Training labels must end strictly before the first validation signal; validation
labels must end strictly before the first test signal. Exact boundary contact is
purged. This uses actual stored exit timestamps, so long/irregular horizons are
handled without assuming a fixed number of rows. Purged row indices are retained
per fold; global holdout-boundary exclusions are retained in the manifest.
Minimum training-session counts apply after purging. Folds without enough
training sessions or with empty validation/test partitions are omitted; no usable
folds is an error. Choose windows long enough to leave observations after purging.

The default embargo is zero: expanding chronology and explicit label-interval
purging already prevent those overlaps. An optional `embargo_days` adds a buffer
of calendar days between label completion and the next partition, and requires
a nonempty `embargo_rationale`. For example, a documented source-publication delay
could justify an additional buffer. The parameter is not an unexplained default
or a guarantee against every form of data dependence.

`manifest.save(experiment_directory)` writes a content-addressed manifest and a
fixed `final-holdout.json` binding. The binding pins dataset hash, final dates and
row indices. Reusing the directory with changed holdout data or boundaries fails
instead of replacing the binding. Keep the original experiment for audit; a
deliberately revised experiment uses a distinct directory. The split does not
itself consume holdout outcomes for tuning, but downstream evaluation must still
respect one-time final assessment.

`fit_training_transform(dataset, manifest, fold_index)` verifies dataset and split
integrity, then fits per-feature mean and population standard deviation using only
that fold's training indices. Constant columns use scale one. Applying
`transform.transform(matrix)` reuses those frozen parameters and rejects invalid
width/nonfinite inputs. It performs no imputation, target encoding or feature
selection. `transform.save(directory)` persists parameters, training row indices
and a training-feature hash; labels never participate in the fit.

This follows the training-only preprocessing rule in scikit-learn's
[common pitfalls guidance](https://scikit-learn.org/stable/common_pitfalls.html).
Unlike a fixed sample-count gap in
[TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html),
the purge boundary here is determined by each label's actual timestamp.

Synthetic tests assert disjoint label intervals across every fold, equality and
long-horizon purging, grouped timestamps, frozen holdout binding, justified embargo
behavior, reproducible manifests and unchanged scaling parameters after replacing
validation/test/holdout feature and target values. Real evaluation and performance
claims remain outside this implementation task.
