# Paper risk reservations and stress scenarios

Apply Alembic migration `0005`. All reservation operations take a PostgreSQL
account-row lock shared with ledger/session accounting. Call them inside
`engine.begin()` and commit before relying on a reservation. Concurrent workers
read the same canonical capacity generation plus committed active reservations;
they cannot each approve against a private stale copy of the remaining budget.

`publish_capacity` stores a reconciled portfolio, matching persisted session,
sizing policy and symbol stress assumptions. Include held positions and external
pending buys in this base snapshot. Managed reservations are added exclusively
from the reservation table. Do not also submit them as external pending orders.
Replacement requires all managed reservations to have confirmed terminal states.
Ledger or session changes invalidate the generation for new reservations.

`reserve` sizes again under lock and records cash, current-to-stop risk, cost
buffers, decision, stress inputs and report. Unique request IDs make retries
idempotent; changed inputs under the same ID fail. Buy/sell charges and gap
allowances enter reserved heat once. Pending purchase cash includes buy charges
once. Profitable holdings still consume marked-price-to-stop giveback risk.
SIZED/reserved is paper capacity only, not dispatch authorization. Recheck all
execution, entry-evidence and risk gates before any future broker submission.

`release` requires a confirmed filled/cancelled/rejected state, actual cumulative
filled quantity and a reconciliation evidence reference. Cancel requests,
timeouts, partial fills and UNKNOWN are not terminal. No timeout or proposal
expiry automatically releases capacity. Repeated identical confirmations are
idempotent. The execution/reconciliation caller is responsible for establishing
that evidence, not merely labeling an unconfirmed request as confirmed.

Any terminal state with fills invalidates base capacity before releasing the
reservation. Reconcile actual cash, fees and holdings and publish a new generation
before new approvals. While partial fills remain nonterminal, retain the full
reservation; ledger changes block further approvals. This conservative first
version pauses entries until outstanding reservations finish and does not yet
support incremental transfer of partial-fill risk to held inventory. Exits and
reconciliation remain available. Restart reloads the same reservations; it cannot
free capacity. A rolled-back transaction creates no durable reservation.

The correlated gate applies `max(configured floor move, ATR multiple × ATR/mark)`
to every held and pending position, capped at total price loss, plus stressed
exit costs and unpaid managed buy charges. Reject if the modeled loss exceeds
the configured percentage of E0. Report separately the supplied historical-shock
scenario and worst single-symbol gap scenario; those reports are not silently
treated as additional summed losses or gates. Every assumption is explicit and
the historical source must identify its evidence (synthetic tests are labeled).
The roadmap's 5%/three-ATR, 2% gate and 20% single-name gap are research examples.

Blocked-exit notional shows exposure remaining when liquidation is unavailable.
These scenarios do not bound real gaps, model recovery time or guarantee fills;
daily data cannot prove intraday execution. Missing stress inputs fail closed.
No live trading is enabled. Tests include three simultaneous PostgreSQL workers
competing for shared cash/risk, idempotency, terminal release, rollback and exact
scenario arithmetic.
