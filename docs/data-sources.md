# Data source inventory and research access decision

Reviewed 2026-10-23. Scope: NSE daily cash-equity research. This inventory approves
an implementation route, not a licence grant or live-market-data entitlement.
No historical price files were collected during this review; observed coverage
is **unavailable** until an authorized file is imported and validated.

## Inventory

| Source | Fields and format | Coverage and freshness | Decision |
| --- | --- | --- | --- |
| User-supplied CSV | Proposed importer contract: exchange, symbol, session_date, open, high, low, close, volume; manifest supplies provider, retrieval time, source hash and adjustment policy | Exactly the validated rows supplied; no implied coverage before/after them. Session dates and gaps must be measured | Approved implementation route for synthetic fixtures or files the user has rights to use; no rights inferred from possession |
| NSE published reports | Official reports catalogue lists CM-UDiFF Common Bhavcopy Final ZIP; exact header/version validation must use the official format package and an authorized sample | Daily report, not executable quotes. Earliest date, missing sessions, delisted-symbol coverage and retrieval latency remain unverified | Local-file parsing may proceed; automated website collection is not approved |
| Optional yfinance | Daily price history, with corporate-action/adjustment options; `auto_adjust` defaults to true in reviewed documentation, so the adapter must set it explicitly | Actual NSE symbols/date ranges not sampled. Missing history must remain missing; documented intraday history restriction is not a guarantee of NSE availability | Optional personal research only, disabled pending applicable Yahoo terms and usage eligibility; no live dependency |
| Broker market/account data | Quote time, executable prices, holdings, orders, fills, charges and market status depend on the selected broker contract | No broker selected, entitlement verified or latency measured | Future dependency; unavailable for live operation |

The local CSV schema above is a project contract, not a claim about an external
provider's raw headers. No source supplies verified point-in-time universe,
corporate-action, historical calendar or event-blackout coverage here.

## NSE evidence and restrictions

The [official reports catalogue](https://www.nseindia.com/all-reports) marks legacy
CM CSV bhavcopy formats discontinued from 2024-07-08 and directs users to UDiFF.
The [official formats page](https://www.nseindia.com/static/resources/forms-formats-members)
provides UDiFF specifications and sample-test packages. A later adapter must
record the selected specification, inspect actual headers and reject unsupported
formats rather than silently using the old CSV layout. Field-level mapping and
actual historical coverage remain unverified in this checkpoint.

The [NSE terms](https://www.nseindia.com/static/nse-terms-of-use), item 9, prohibit
systematic or automated website data collection. Therefore this project does
not authorize scraping, hidden API collection or archive enumeration. The
[copyright policy](https://www.nseindia.com/static/nse-copyright) describes limited
personal/non-commercial/educational copying with attribution and conditions;
that does not override the collection restriction or grant general redistribution.
Keep raw third-party data outside Git and do not publish it in fixtures or reports.

No numeric automated request quota was established. The configured website
request allowance is zero. A future permitted feed requires documented access
rights and its own throttle contract; a convenient URL is not such a contract.
Do not work around a CAPTCHA, login, 401/403, blocked session or other access
control. After a 429, honor Retry-After and pause; never rotate identities or
proxies to evade limits. This guidance does not authorize making requests now.

## Optional Yahoo/yfinance evidence

The [maintainer's repository](https://github.com/ranaroussi/yfinance) describes
yfinance as unaffiliated with Yahoo and intended for research/education, and
points to Yahoo's terms for data rights. Its software licence does not license
market data. The [download API documentation](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
documents daily intervals, adjustment options, exclusive end dates and a general
60-day intraday-history limit. Do not infer complete 60-day NSE intraday coverage
or free real-time quotes from that limit or from a streaming API feature.

Numeric service quotas, redistribution rights and this project's applicable
Yahoo usage eligibility have not been established. Keep network use off until
reviewed, cache only when permitted, and record provenance and explicit adjustment
parameters. Empty responses, missing symbols and shortened ranges are coverage
failures, not zero prices. Do not use this source for execution-grade risk marks.

## Required import manifest and coverage evidence

Each import must record source/provider, original filename, SHA-256, acquisition
time in UTC, permitted-use basis, format version, adjustment policy, requested
range and observed first/last sessions. Report expected versus present sessions,
duplicates, rejected rows and missing instruments separately. Never substitute
current index membership for historical membership or fabricate absent history.
Synthetic fixtures must identify themselves as synthetic and use invented symbols.

Approved now: offline software development and synthetic tests. Conditionally
eligible: authorized local CSV/NSE files after provenance and rights checks.
Unavailable: automatic NSE website collection, verified complete historical
coverage, free real-time NSE quotes, broker data and live execution.

## Acceptance review

- Official NSE catalogue, terms, copyright and format links are recorded above.
- Unobserved history and raw NSE headers are explicitly unverified.
- No real-time quote availability is claimed; daily research stays separate.
- No access-control bypass or unauthorized automated collection is permitted.
- Broker entitlement and execution-grade freshness remain future live dependencies.

Run `uv run --locked pytest` for repository documentation-link integrity alongside
the regression suite. These tests validate repository consistency, not provider
availability or legal entitlement. Re-review terms before enabling any adapter.
