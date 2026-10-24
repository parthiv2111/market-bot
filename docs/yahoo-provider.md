# Optional yfinance research adapter

Install with `uv sync --locked --extra yahoo`. The dependency is optional and
imported lazily. CI uses injected responses, never Yahoo requests or API keys.
No application startup path or execution component enables this adapter.

`YahooResearchProvider(cache_directory, ticker_map, enabled=True, usage_basis=...)`
requires explicit enablement, an asserted permitted-use basis and an unambiguous
NSE symbol-to-Yahoo `.NS` ticker mapping. There is no automatic ticker guessing.
The default is disabled. Eligibility must be reviewed before network use under
the [source inventory](data-sources.md); setting a flag does not grant data rights.

Calls request daily data with explicit `auto_adjust=False`, `back_adjust=False`,
`repair=False`, `actions=True`, `keepna=True`, `threads=False` and a ten-second
timeout. The inclusive provider request end is converted to Yahoo's exclusive
end date. See the [maintainer API documentation](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html).

Raw fields preserve OHLC returned with those flags; this does **not** reconstruct
historical pre-split tape prices. Adjusted close is retained separately. Adjusted
open/high/low use the explicit ratio Adj Close / Close, using Decimal arithmetic.
Missing/nonfinite adjustments and invalid OHLCV fail rather than copying raw
prices into adjusted fields. Prices retain the provider's precision and data
limitations; corporate-action correctness requires independent validation.

The result uses the same DailyProvider contract as CSV, including UTC acquisition
time, normalized content hash, provider identity and per-symbol coverage. Cache
keys include mapped tickers, requested range and adjustment settings. Cache TTL
defaults to one day, and reads verify metadata and content hash. Cache hits
preserve the original acquisition time; expiry refetches without silently serving
stale data. Invalid caches fail for explicit operator review. An empty provider
frame is reported as missing coverage, never fabricated prices.

Timeout/connection exceptions receive at most three attempts (default two), with
bounded exponential waits. Other access/library failures are not retried or
bypassed. The yfinance library may convert some provider failures to an empty
frame; those remain missing coverage. This is not an execution-grade availability
or freshness guarantee. No account, order or risk-monitor path depends on it.

The [yfinance project](https://github.com/ranaroussi/yfinance) describes its
research/educational role and Yahoo personal-use restriction; its software licence
does not license the downloaded data. Redistribution rights, numeric request
quotas and actual NSE history coverage remain unverified. Do not use this adapter
for commercial redistribution or free real-time NSE quote claims. Respect
provider denials and limits; never rotate identities or proxies to evade them.

Run `uv run --locked pytest tests/test_yahoo_provider.py` for fully mocked
semantics, mapping, explicit flags, cache expiry/corruption, bounded retries,
schema changes and missing data checks.
