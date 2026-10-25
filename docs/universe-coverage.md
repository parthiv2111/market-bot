# Dated universes and evaluation coverage

`prediction_bot.universe` accepts supplied JSON constituent snapshots, not a
download of today's index membership. Each `Universe` has a name, nonoverlapping
`snapshots` and optional explicit history `limitations`. A snapshot contains
`valid_from` (inclusive), `valid_to` (exclusive), timezone-aware `known_at`, source
attribution and members with stable `instrument_id` and contemporaneous `symbol`.
Gaps are allowed but produce unavailable coverage; no membership is extrapolated.
Empty membership must be represented by an explicit empty snapshot.

```python
from datetime import UTC, date, datetime
from pathlib import Path

from prediction_bot.universe import UniverseStore

store = UniverseStore(Path("data/universes"))
version = store.import_file(Path("data/permitted-constituents.json"))
universe = store.get(version)
members = universe.members(date(2026, 1, 2), as_of=datetime(2026, 1, 2, 10, tzinfo=UTC))
```

The snapshot must have been known at the requested cutoff and effective on that
date. A current list cannot be backdated to fill missing historical membership.
Removal from a later snapshot does not erase earlier membership. Aliases are
checked against the reference bundle's effective-dated instrument IDs. A symbol
rename requires an updated snapshot and reference alias; data availability is
keyed by stable instrument ID so earlier history survives the rename.

Use `coverage_report(universe, reference, observations, start=..., end=...,
as_of=..., history_sessions=..., future_sessions=...)` before evaluating a period.
It selects constituents separately at each session close. It requires observations
for the current session plus the requested prior open sessions and for the next
requested open sessions (default five). Closed dates are skipped; absent calendar
coverage, incomplete windows, missing identities and missing/late observations
are explicit reasons for insufficiency. An unfinished current session cannot pass.

`AvailableObservation` records an instrument ID, session date and trustworthy
knowledge timestamp from ingestion. Historical feature observations must have
been known at the evaluation session close; future label observations need only
be known by the report cutoff. This is a conservative close-time contract: data
first acquired the following day will not qualify for same-close features. A
next-session strategy needs a separately specified decision-time contract; do not
backdate knowledge timestamps to improve coverage.

When delisted history is unavailable, include a `HistoryLimitation` with the stable
instrument ID, knowledge timestamp and reason `missing_delisted_history`. The
report exposes this limitation even when the security is absent from later lists.
Index removal alone does not prove delisting, so no delisting status is inferred.
Missing dates and symbols remain in the report; no forward filling or automatic
exclusion makes a deficient sample appear complete. Limitations are audit metadata,
not trading features.

`CoverageReport.save(directory)` writes an immutable content-addressed JSON report
with per-date/symbol results, input hashes, cutoff, date range and window lengths.
Preserve the referenced universe, calendar and observation input files alongside
the report. Constituent stores validate hashes when reloading snapshots. No real
historical constituent files or delisted price history are bundled; tests use
invented membership changes and calendars. The module supplies the selector for
the later backtest engine and does not claim a completed historical evaluation.
