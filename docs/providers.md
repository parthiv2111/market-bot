# Daily provider interface and CSV ingestion

Providers implement `DailyProvider.fetch(FetchRequest) -> ProviderResult`.
Consumers use that result without knowing whether it came from a local file or
a future permitted network adapter. Fetching only parses data; it does not write
database records, infer stable instrument IDs, submit orders or fill missing bars.

CSV files use UTF-8 (optional BOM), comma separation and the following columns:
`exchange,symbol,session_date,open,high,low,close,volume`. Column order may vary.
The only optional columns are all four of `adjusted_open,adjusted_high,adjusted_low,
adjusted_close`. Unknown/duplicate columns and uneven rows are rejected with
schema or line-number diagnostics. No raw input values are echoed in errors.

Exchange must be NSE; dates use YYYY-MM-DD. Prices are finite positive Decimals,
volume is a nonnegative integer, and OHLC must fit inside low/high. Each
exchange/symbol/date key is unique within a source. Adjusted values are separate
from raw prices; each row supplies all four or none. Missing adjustments remain
None, never a silently copied raw value. The result labels supplied adjustments
but does not establish their correctness; later corporate-action validation is
required. Provider results retain the source row count before request filtering.

```python
from datetime import date
from pathlib import Path
from prediction_bot.providers import CSVProvider, FetchRequest, export_csv

provider = CSVProvider(Path("data/authorized.csv"), usage_basis="owner-authorized research")
result = provider.fetch(FetchRequest(("SUPPLIED_SYMBOL",), date(2026, 1, 1), date(2026, 1, 31)))
canonical_bytes = export_csv(result.bars)
```

Requests use inclusive dates and explicit symbols. Pass `expected_sessions` from
a verified calendar to calculate missing/unexpected dates for every requested
symbol, including symbols with no observations. When no calendar is supplied,
missing/unexpected coverage is None rather than falsely reported complete.
The importer validates the entire file before filtering, and fails as a unit on
any malformed row. No partially accepted result is returned.

Provenance records provider, basename, SHA-256 of the original bytes, UTC fetch
time, usage basis and adjustment policy. Usage text is an assertion, not a licence
verification. An explicit aware `fetched_at` can be supplied for deterministic
fixture/replay metadata; otherwise actual read-time is used. Source hash and
canonical exported hash can differ due to column/row ordering or newline changes.

`export_csv` emits sorted rows, LF newlines, decimal text and empty adjustment
cells, then validates the output. Export/reimport/export is byte-reproducible.
Tests use invented symbols only and cover replaceable providers, empty/partial
coverage, malformed schemas, invalid prices/dates, duplicate rows, raw/adjusted
separation and reproducible round trips. Run `uv run --locked pytest tests/test_providers.py`.
