# NSE cash report import

Parser version `nse-cm-udiff-2026-06-30-v1` uses the 34 ordered ISO headers in
the official [UDiFF format workbook](https://nsearchives.nseindia.com/web/mediaattachment/2026-06/UDiFF_trade_and_Bhavcopy_file_formats_20260630115803.xlsx),
linked from [NSE Forms & Formats](https://www.nseindia.com/static/resources/forms-formats-members).
The workbook was inspected on 2026-10-24: sheet `Bhavcopy File Format`, rows 2–35,
column D specifies headers; rows 9–10 define symbol/series, rows 16–19 OHLC and
row 26 traded volume. The `Standard Value List_latest` sheet identifies STK as
cash-market stock and NSE as the source. Tests generate synthetic values using
these verified headers; no actual market report is redistributed in this repository.

Supported inputs: authorized local CSV or ZIP containing exactly one CSV, at
most 32 MiB compressed and uncompressed. Archives are read in memory and never
extract filesystem paths. The parser accepts only NSE/CM/STK rows, final primary
session F1 and the explicitly supplied source date. Changed headers, interim or
second-session reports, unsupported segments and malformed rows fail clearly.
This initial version deliberately rejects unverified report variants.

`NSERecord` retains series, exchange token and ISIN alongside the raw Decimal
OHLCV bar. Series are not collapsed: EQ and BE remain distinguishable. Consumers
must select their permitted series explicitly. No adjustments are invented. Rows
with zero traded quantity are counted separately and do not become fresh prices.
Daily reports cannot establish intraday prices or threshold ordering.

```python
from datetime import date
from pathlib import Path
from prediction_bot.nse_reports import import_report

report = import_report(
    Path("data/user-downloaded-report.zip").read_bytes(), date(2026, 1, 2), Path("artifacts/nse")
)
```

The original bytes are stored under SHA-256 with an immutable manifest containing
source date, parser version and row counts. Repeat imports are idempotent and
existing mismatched archives fail integrity checks. Candle database insertion is
left to ingestion jobs; parsing never overwrites stored market observations.

Network access is dependency-injected through `PermittedTransport`, not implemented
as an NSE website scraper. `fetch_permitted_report` requires an exact HTTPS URL
allowlist and no credentials in the URL. An allowlist must reflect actual source
permission; it does not grant that permission. There are no approved NSE website
URLs under the current [access decision](data-sources.md).

Non-200 statuses and connection/timeout failures persist a failure checkpoint and
raise `ResumableFetchError`. HTTP status and Retry-After are retained. No automatic
retry, redirect fallback or access bypass occurs. The caller may explicitly resume
only after the access problem is resolved or the retry delay has elapsed. Transport
implementations must honor size/time limits, disallow unapproved redirects and
avoid logging credentials. The default importer makes no network requests.

Run `uv run --locked pytest tests/test_nse_reports.py`. Tests cover accurate
OHLCV/series, source/date/schema restrictions, ZIP validation, immutable source
retention, no-trade rows and resumable 401/403/429/server failures.
