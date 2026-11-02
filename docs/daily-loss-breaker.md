# Daily percentage loss circuit breaker

Apply migration `0006`. `SizingPolicy.daily_loss_limit_pct` is required, finite,
positive and expressed in percentage points. Publishing reconciled paper capacity
binds it and E0 to the exchange session. Neither can change during that session;
the database also protects triggered controls against editing or deletion.

Use total net marked session P&L, including unrealized changes and fees, from
session accounting. The cutoff is `E0 * percentage / 100`; trigger when
`max(0, -daily_pnl) >= cutoff`. At E0 100,000 and 1%, losses of 999, 1,000 and
1,250 respectively stay below, trigger, and trigger with 250 overshoot. Store
the trigger timestamp, frozen policy values, P&L, cutoff, reason and overshoot.
Realized +300 and unrealized -1,400 produce a net -1,100 for this calculation.

Every persisted reconciled equity observation evaluates the bound control in the
same transaction and account lock. Reservations evaluate it again, including
idempotent retries. `entry_dispatch_allowed` rechecks the latch, reservation,
expiry, freshness and unchanged ledger/session snapshot for queued paper entries.
It is a gate for the future execution manager, not a broker call or a guarantee
that a check remains valid after releasing the transaction lock. Dispatch must
coordinate with this lock and its durable order lifecycle.

Before a stop, sizing subtracts current/pending heat from remaining daily loss
headroom; positive intraday P&L does not replenish that budget. A stop invalidates
capacity and atomically creates idempotent CANCEL_ENTRY intents for active
reservations and a LIQUIDATE_ACCOUNT intent. Consumers must reconcile late fills,
cancel entries, and reduce current owned inventory when executable. Initial
holdings are diagnostic only; re-read actual holdings before every exit. Preserve
protective exits until closure is confirmed. Intents are not acknowledgements,
filled orders, or a claim that the account is flat. Blocked exits remain pending.

The latch survives rebounds, funding, reloads and policy changes. This version
provides no reset endpoint and conservatively blocks later sessions after a
trigger until a separately reviewed recovery workflow is implemented. Other
weekly/monthly/operational latches still apply. Missing configuration, stale
capacity and changed accounting state block entries. Call the gate on heartbeat
and before dispatch; callers must commit stop-producing transactions even when
the result rejects an entry. Rolling back a whole transaction rolls back its
observation and stop together.

Equity must use fresh conservative marks and consistent remaining-cost estimates.
Stale observations cannot authorize trading. Gaps and unavailable exits can cause
losses beyond the cutoff; this is a continuation-of-trading control, not a loss
guarantee. Live trading remains disabled. PostgreSQL tests exercise exact
thresholds, queued entries, immutable stops/intents and rebound/deposit persistence.
