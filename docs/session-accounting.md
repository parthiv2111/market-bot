# Session baselines and cash-flow-neutral drawdown

Apply Alembic migration `0004`. `session_accounting.persist_observation` records
an immutable equity observation and its resulting `SessionState` in one caller
transaction. The per-account row lock also used by fill ingestion serializes
updates. `read_session` reloads the latest baseline, peak, counters and latches
after restart. Observation IDs make retries idempotent; changed payloads or
calendar inputs under the same ID fail. Neither funding nor replay clears stops.

An `EquityObservation` supplies reconciled net equity, cumulative external
deposits minus withdrawals, an aware timestamp, and explicit freshness and
reconciliation flags. Obtain equity from the portfolio ledger, then apply a
consistent conservative valuation and subtract any unpaid charges, liabilities
and estimated remaining liquidation costs. Paid fill fees are already in the
ledger and must not be subtracted again. The caller must establish freshness;
this accounting module does not fetch quotes or broker state.

At the exact supplied exchange opening, submit `session_open=True`. Store that
valuation as E0 and its cumulative flow total as the daily flow baseline. Daily
P&L is current equity minus E0 minus subsequent net funding. Weekly and monthly
P&L have independent opening equity/flow baselines. ISO weeks and calendar months
use the Asia/Kolkata exchange date. Their baselines reset only at the first open
session in a new period; server midnight and closed dates never trigger resets.
The first observation initializes account-inception period baselines; it cannot
claim a full prior week/month of history. Import earlier observations when that
history is needed. Skipped session openings fail closed rather than inventing E0.

The lifetime unit index starts at 1. For trading changes it multiplies by new
equity divided by previous equity. A pure cash-flow observation changes capital
without changing the index; its lifetime peak never resets. Drawdown is
`(peak - index) / peak`. Funding events must have separate before/after valuations:
an observation combining cash flow and a trading valuation change is rejected.
Submit each flow separately at its actual time. Funding latches a reconciliation
pause and cannot enlarge an already-fixed session denominator. Zero equity
records total drawdown and a stop reason; subsequent funding needs explicit
recovery instead of restarting the index.

Daily entry and completed-campaign counters reset at session opening. Consecutive
losses persist across days, increase on a completed losing campaign, and reset on
a completed win/flat campaign. A campaign is a completed position lifecycle, not
each partial fill. Caller-supplied stop reasons are additive and durable; this
module intentionally provides no recovery/clear-stop operation. Later risk-state
policies must authorize and audit any recovery. Stale/unreconciled observations
are rejected without changing the profit high-water mark or persisted state.

Opening valuations and subsequent observations may fall within the supplied
session including the closing boundary. Live trading remains disabled. Tests use
synthetic exchange sessions and money flows, with PostgreSQL restart/retry checks.
