# Deterministic daily indicators

`features_at(observations, reference, symbol=..., session_date=..., as_of=...,
config=IndicatorConfig())` returns an `indicators-v1` feature vector, with a value
and explicit invalid reason for every feature. Supply `KnownBar(PriceBar(...),
known_at)` observations with actual ingestion knowledge timestamps and a verified
calendar. The return object records configuration, date, cutoff and raw price basis.

Only the selected symbol's bars on or before the target date and known by the
cutoff participate. The target must have completed according to the supplied
calendar; completed daily observations cannot claim knowledge before their close.
Future rows are excluded before validation, so even malformed future bars cannot
change an earlier result. Historical runs should call this function separately
with each decision's actual cutoff. A later cutoff can legitimately see later
arriving historical data, but must not be substituted into earlier evaluations.

The implementation validates raw OHLCV, rejects duplicates, sorts dates and
restarts every warmup after a missing open session. Closures do not count as gaps.
Unknown calendar coverage, missing target bars and unfinished sessions produce
invalid vectors. Prices outside the supported floating-point computation range
(1e-100 through 1e100) and volume over 1e100 produce `numeric_overflow`, not NaNs.

The following conventions are explicit so comparisons with other packages can
use the same seeds and warmups:

| Feature | Definition and first valid sample |
| --- | --- |
| return | Raw close divided by the close `return_period` sessions earlier, minus one; needs period + 1 closes. |
| sma | Mean of the most recent `sma_period` closes. |
| ema | Seed with the first period's SMA, then update with alpha = 2 / (period + 1). |
| rsi | Seed average gain/loss with the first period close changes, then Wilder smoothing with alpha = 1 / period; needs period + 1 closes. All gains gives 100, all losses 0, a flat series 50. |
| macd | Fast EMA minus slow EMA; defaults 12 and 26. Signal is an SMA-seeded 9-period EMA of valid MACD values; histogram is MACD minus signal. |
| atr | True range is the maximum of high-low and distances from previous close to high/low. Seed with period true ranges, then Wilder smoothing; needs period + 1 bars. The first bar has no true range. |
| volatility | Population standard deviation (ddof=0) of the last period simple one-session returns; unannualized and needs period + 1 closes. |
| volume_sma | Mean of the last period raw volumes including the current bar. |
| relative_volume | Current raw volume divided by the preceding period's mean, excluding current volume; a zero baseline is explicitly invalid. |

Default SMA/EMA/volatility/volume periods are 20; RSI/ATR are 14. A valid zero is
distinct from an insufficient-history result. No annualization, forward fill,
future-centered rolling window or automatic adjustment is performed. Vendor
adjusted prices are ignored. Raw-price indicators can reflect corporate-action
discontinuities; action-adjusted features require a separately versioned
point-in-time transformation before being introduced as a new feature basis.

Formula references: Fidelity's [EMA definition](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/ema),
[RSI definition](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/RSI),
[ATR definition](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr)
and [MACD definition](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/macd).
Flat-RSI, initial-ATR, volatility and volume conventions above are this project's
explicit choices, not claims of identical output from every charting package.

The hand fixture uses closes 10, 12, 11, 14, 13 with high/low one unit away and
volumes 100, 200, 300, 400, 500. With periods three and MACD 2/3/2, final EMA is
12.75, RSI is 200/3, ATR is 8/3, MACD and signal are 0.25, and relative volume is
5/3. Tests compare all features at absolute and relative tolerance 1e-12, verify
seed boundaries and change/append future bars to check historical invariance.
