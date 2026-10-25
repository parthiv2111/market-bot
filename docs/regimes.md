# Versioned market-regime rules

`regime_at(observations, reference, index_symbol=..., equity_symbol=...,
session_date=..., as_of=..., rules=RegimeRules())` derives both symbols' indicator
vectors at the same historical cutoff and classifies their combined regime.
Provide distinct index/equity symbols, completed `KnownBar` observations and a
verified calendar. `classify_regime` can also consume previously generated feature
vectors, but requires matching dates, cutoffs, configuration and implementation.

Rules version `regimes-v1` uses the relative spread `EMA / SMA - 1` for trend.
Both index and equity must have spreads at least the positive threshold for
`bull`, or at most the negative threshold for `bear`. Other combinations are
`range`, including disagreement. If either symbol's unannualized population
return volatility is at or above its threshold, `high_volatility` takes precedence;
the separate trend label is retained. Equality is included at each threshold.

Research defaults use EMA 10, SMA 30, spread threshold 0.01 and volatility threshold
0.03. These are explicit illustrative rules, not validated trading parameters.
All indicator warmup, knowledge-time and gap rules described in
[indicators](indicators.md) apply. No future bar participates in a historical
classification.

Missing, stale, incompatible or invalid required features produce `unknown` with
specific reasons and `entry_eligible=false`. Unknown cannot appear in the rule
allow-list. The default `entry_regimes` is empty, so the classification itself
authorizes no entries. Explicitly choosing allowed known regimes is a research
policy decision; eligibility never places an order or bypasses other risk checks.
The broader system's live execution remains disabled.

Each `RegimeDecision` records the date/cutoff, index and equity symbols, required
indicator values, derived trend spreads, feature implementation version, rule
payload and its SHA-256 version. Changing any rule or indicator parameter changes
the version. `decision.save(directory)` atomically stores the complete decision
under its content hash, allowing the classification to be audited and reproduced.

Synthetic tests cover bull, bear, range, high volatility, disagreement, missing and
insufficient history, incompatible features, rule-version changes and invariance
when future bars are appended or modified. No performance claim follows from
these functional tests.
