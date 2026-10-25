# Candle quality and corporate actions

`QualityStore` is the local validation boundary for canonical provider CSVs. It
keeps the exact source bytes under `raw/`, accepted observations and quarantine
diagnostics under `validation/`, supplied action records under `actions/`, and
derived research snapshots under `research/`. All files have content-addressed
names and are published atomically without overwriting existing content.
Repeating an identical import with identical metadata returns the same report.
The [ingestion jobs](ingestion.md) consume validated batches and write raw candles
transactionally, rejecting conflicting deliveries. The quality store itself does
not write to the candle table.

```python
from datetime import UTC, date, datetime
from pathlib import Path

from prediction_bot.data_quality import QualityStore
from prediction_bot.providers import FetchRequest

store = QualityStore(Path("data/quality"))
request = FetchRequest(
    ("SYNTH",),
    date(2026, 1, 2),
    date(2026, 1, 5),
    expected_sessions=(date(2026, 1, 2), date(2026, 1, 5)),
)
batch = store.import_csv(
    Path("data/permitted.csv").read_bytes(),
    request,
    known_at=datetime(2026, 1, 6, tzinfo=UTC),
    usage_basis="user-supplied permission",
)
series = store.research_series(batch, (), as_of=datetime(2026, 1, 6, tzinfo=UTC))
```

Supply expected sessions from a verified calendar. Missing observations remain
explicit gaps; no forward fill occurs. Prices must be finite, positive and
consistent with OHLC bounds; volume must be a nonnegative integer. Unexpected
symbols/dates, non-session dates, and incomplete current-day observations go to
quarantine with their original cells and line number. Every occurrence of a
duplicate candle identity is quarantined, including conflicting values. A broken
encoding, CSV envelope or header quarantines the whole source rather than
accepting an ambiguous partial file. Preserve the source and report together.

`CorporateAction` requires a unique event ID, source, symbol, effective timestamp,
knowledge timestamp and explicit source-supplied price factor. For a split, also
provide the new-shares-per-old ratio; its reciprocal must equal the price factor.
For a cash dividend, supply the authoritative backward price factor, with a share
ratio of one. This API does not infer dividend factors from current prices or
implement entitlement/cash accounting. Resolve corrected/duplicate event IDs
explicitly before calling it. Symbol changes require point-in-time identity
resolution before associating actions; the adapter does not guess aliases.

Research snapshots apply factors only to dates strictly before the event's local
effective date, and only after both its knowledge and effective timestamps are
at or before `as_of`. Later announcements and future events cannot change an
earlier feature snapshot. Raw observation knowledge must also precede the cutoff:
a file fetched today cannot be represented as information known years ago.
Imports conservatively accept only prior-day candles. Trustworthy knowledge
timestamps must come from ingestion records, never invented backdated values.

Raw OHLC and volume remain unchanged. The four adjusted price fields are computed
in a separate snapshot; volume is always raw. Vendor-adjusted fields are retained
in the original source but ignored when computing these point-in-time prices,
because their historical knowledge cutoff is unproven. A snapshot records its
validation version, action version and cutoff for reproducibility. No live order
placement is introduced.

Tests cover splits, dividends, future and delayed knowledge, effective-date
boundaries, corrupt prices/volume/dates, gaps, duplicate rows/imports, source
integrity, malformed files and separation of raw from derived observations.
