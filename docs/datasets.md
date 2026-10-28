# Executable daily labels and dataset snapshots

`build_dataset(observations, universe, reference, actions, start=..., end=...,
as_of=..., policy=LabelPolicy(...), feature_schema=(...), indicators=...)` builds
long-only daily research rows. Intraday labeling is explicitly unsupported.

The signal timestamp is the supplied exchange session close. Features use only
completed bars known by that instant, and membership is selected as of that date.
Entry is the next open session's open; exit is the fifth following open session's
close, counting entry as session one. Holidays do not count. The signal bar's
close is never used as the entry fill. All five future daily observations must
exist and be known by the dataset creation cutoff; incomplete tail rows and gaps
are excluded with reasons. These are hypothetical daily-bar execution assumptions,
not evidence that a specific order could fill at those prices or volume.

Fees and slippage per side, in basis points, are required policy inputs without
implicit defaults. For fractions `f = fee_bps / 10000` and
`s = slippage_bps / 10000`, paid value is `entry_open * (1+s) * (1+f)` and received
value is `exit_close * (1-s) * (1-f)`. The target net return is received / paid - 1;
the binary `positive` target is one only when net return is strictly positive.
Use an explicitly reviewed all-in cost assumption; this module does not infer
current broker fees, taxes or market impact. No leverage, stops or intraday
execution is modeled.

Provide an explicit corporate-action ledger, even if empty, and its verified
coverage dates/source in `LabelPolicy`. Windows outside that coverage are
excluded. Known corporate actions during the holding period are also excluded:
raw-price returns would otherwise misstate split/dividend effects, and this label
version does not model share entitlements or dividend cash. Actions effective at
or before entry need no held-position adjustment. Future action knowledge after
the dataset cutoff is not used. Keep actual source evidence with the snapshots;
declaring coverage is not a substitute for obtaining it.

Rows contain metadata, a fixed tuple of feature values, and a separate target
object. Feature schemas accept only names from the indicator implementation;
target names and arbitrary extra columns are rejected. `feature_matrix()` reads
only the feature tuples, so target changes cannot alter the matrix. Never flatten
targets into a training frame and then select features by exclusion.

`snapshot.save(directory)` atomically writes a content-addressed JSON artifact.
It stores feature and label schemas, included rows, exclusion reasons, policy and
indicator configuration, and hashes of observations, constituents, calendar and
corporate actions. Preserve those source inputs and the Git revision with the
artifact. Labels can overlap in time; chronological splitting and purging belong
to the subsequent modeling/validation work, not this dataset builder.

Tests hand-check a signal on synthetic January 3 with entry January 5 and exit
January 10 over five supplied sessions (5, 6, 8, 9, 10). An entry open of 120 and
exit close of 150 are adjusted for explicit per-side costs. Further tests cover
missing intermediate/tail observations, late signal data, action exclusions,
target-column rejection, future-price invariance of features and deterministic
snapshot hashes. No real dataset performance is claimed.
