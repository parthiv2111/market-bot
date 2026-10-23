# Instrument identity and supplied session calendars

`prediction_bot.reference_data` loads validated, versioned JSON bundles containing
instrument aliases and explicit NSE session dates. It does not download schedules
or assume that every weekday trades. Use authorized supplied reference files;
the test calendar is synthetic, including an invented Sunday session.

An alias records stable `instrument_id`, `exchange`, `exchange_id`, `symbol`,
`valid_from` and exclusive `valid_to`. Adjacent intervals allow a rename without
changing stable identity. Overlapping symbol, exchange identifier or stable
identity intervals are rejected. `bundle.resolve(symbol, on=date(...))` resolves
the identity on that date; current symbols never overwrite historical aliases.
Identifiers are supplied, not inferred from a ticker spelling or today's list.

A bundle declares `source`, timezone-aware `published_at`, inclusive
`coverage_start`/`coverage_end`, `aliases` and `days`. Every date in coverage must
appear exactly once, including weekends and holidays. Each day has `session_date`,
`status` (`open`/`closed`) and a `reason`. Open days require timezone-aware
`opens_at`/`closes_at` on the same Asia/Kolkata date; closed dates have neither.
Special weekend sessions are supported by explicit open entries. Multiple
disjoint trading windows in one date are not supported by this initial model.

`session_at(aware_datetime)` converts the event's date to Asia/Kolkata and treats
the open boundary as inclusive and close as exclusive. A covered closed date or
an event outside session hours returns None. An uncovered date raises LookupError;
it is not silently classified as closed. `sessions_after(date, count)` counts
explicit open dates and fails if coverage ends before the requested horizon.

`observation_status(date, observed=False)` distinguishes `missing_observation`
on an open date from `market_closed`. Data on a closed date returns
`unexpected_observation`, which ingestion must investigate rather than accepting
as a normal bar. Presence on an open date returns `observed`.

```python
from datetime import date
from pathlib import Path
from prediction_bot.reference_data import ReferenceStore

store = ReferenceStore(Path("artifacts/reference"))
version = store.import_file(Path("data/authorized-reference.json"))
bundle = store.get(version)
identity = bundle.resolve("SUPPLIED_SYMBOL", date(2026, 10, 23))
```

Snapshots are persisted as immutable SHA-256-addressed JSON files. They survive
restart, reject corruption and preserve earlier calendar revisions. Record the
reference version in every dataset/replay job. For point-in-time research select
a version published no later than the decision time; merely resolving an alias's
effective date does not establish when that information became known.

This reference-file layer is separate from SQL candle storage. Its stable IDs
must be mapped explicitly to database instruments by ingestion; strings are not
silently cast to database primary keys. No real historical calendar is bundled,
so production date coverage remains unavailable until a verified file is supplied.

Acceptance cases in `tests/test_reference_data.py` cover a rename boundary,
holiday, weekend, special session, UTC/Kolkata boundary, missing observations,
unknown coverage, ambiguous aliases, invalid times, version persistence and
corruption. Run `uv run --locked pytest tests/test_reference_data.py`.
