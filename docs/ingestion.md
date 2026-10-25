# Historical and daily ingestion jobs

`prediction-bot ingest plan.json --reference reference.json --directory data/ingestion`
runs permitted, per-symbol canonical CSV imports into a migrated PostgreSQL
database. Set `PREDICTION_BOT_DATABASE_URL` in the process environment. Credentials
are not accepted as CLI arguments or included in the report. Apply migrations
using the existing database setup instructions first. The job does not download
NSE website data or enable live orders.

Example plan (use absolute source paths):

```json
{
  "revision": "authorized-source-delivery-001",
  "mode": "historical",
  "start": "2026-01-02",
  "end": "2026-01-05",
  "sources": {"SYNTH": "D:/research/permitted/SYNTH.csv"},
  "usage_basis": "Replace with the actual permission for these files",
  "provider": "local_csv",
  "max_attempts": 3
}
```

Use a supplied `ReferenceBundle` JSON containing verified calendar coverage and
instrument aliases. Dates above are illustrative, not an exchange calendar.
Each source must contain only that symbol and the requested date range. Providers
can export their result into the canonical CSV format before this job. Neither
remote downloads nor provider fallbacks happen implicitly.

For daily operation use `mode: daily` with equal start/end dates for the completed
target session. Invoke the command from your existing scheduler on the next day;
the job excludes closed dates using the supplied calendar and skips a wholly
closed range without reading source files. Unknown calendar coverage fails closed.
Special open sessions are included even on weekends. This command does not install
an operating-system schedule. It intentionally does not import intraday or
current-day candles.

The plan plus reference content identifies the job. Under `jobs/<job-id>/`,
`plan.json` preserves both inputs, each hashed-symbol checkpoint records status,
attempt count, source hash, observation knowledge time, validation version and
database run ID, and `report.json` summarizes every symbol. Exit codes are 0 for
success/holiday skip, 1 for per-symbol failures and 2 for setup/plan errors.
Failures include identity gaps, missing candles, quarantined rows and database
conflicts. A failure for one symbol does not conceal other symbols' outcomes.

The first successful read pins the exact bytes in the quality store before any
database writes. Resume reuses those bytes and their original knowledge timestamp,
even if the external file changes. No expiry or refresh happens inside an existing
job: use a new revision to ingest a corrected/new delivery. Source hashes and
validation versions link to the separate raw/quarantine artifacts described in
[data quality](data-quality.md). Preserve the entire ingestion directory with the
database backup; use a different directory per database target. Snapshots pin
external source content, not the application implementation: preserve the Git
revision and locked dependencies alongside a reproducible deployment.

Source I/O failures retry at most three times per invocation with 1- and 2-second
backoff. Semantic/quality/database errors remain visible for explicit correction
and rerun. Database disconnections are not retried automatically because they can
invalidate the session lock. A PostgreSQL session advisory lock prevents two CLI
processes from running the same job concurrently and is released on disconnect.
Direct Python callers must serialize calls for the same job.

Each symbol's candles and completed ingestion-run row commit in one transaction.
Crashes before commit roll back; crashes after commit but before checkpoint safely
replay the deterministic run ID. Identical existing candles are no-ops, with their
original observation timestamp and provenance retained. Conflicting values abort
that symbol's transaction; the attempted source and failure report remain for
reconciliation. Automatic corrections/overwrites are deliberately unsupported.
Prices exceeding database precision fail rather than being silently rounded.

Tests exercise retries, interruption after snapshot creation, database replay
after commit, idempotence across deliveries, conflict rollback, holiday skipping,
unknown calendar coverage, quarantine propagation and immutable cached sources.
