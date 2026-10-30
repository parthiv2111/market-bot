# Research strategy proposals

`prediction_bot.strategy.propose` is a pure function accepting a validated
`ModelSignal`, a `RegimeDecision`, an `InventorySnapshot`, a `ReferenceBundle`, an
explicit `StrategyPolicy`, and an aware `now`. It returns an immutable paper
`Proposal`; it has no broker, transport, or execution method.

Supply model probabilities from the baseline artifact, the completed session's
raw close and ATR, and a matching regime decision. All policy thresholds and age
limits are required research settings, not validated guarantees. BUY proposals
carry an entry band around the reference close, an ATR-based invalidation stop,
the next session's entry window, and the fifth following session's planned exit.
Quantity remains unset until independent risk approval and sizing. Prices still
need exchange tick validation at execution time.

The five-day UP probability estimates positive net return for the dataset's
next-open to fifth-session-close label. It is **not target-before-stop
probability**. The label does not model the path to a target or an intraperiod
stop; adding a stop does not transform the probability into a hit probability.

Malformed models (nonfinite/out-of-range probabilities, naive timestamps,
negative inventory, invalid policy) raise validation errors before proposal
generation. Validly shaped but stale, future, mismatched, incomplete, neutral,
or expired inputs return explicit `NO_TRADE` reason codes. Signals must identify
an exact completed session close. Calendar coverage must include all five future
sessions, including for SELL; missing coverage fails closed. A proposal expires
at the next session close, with that boundary excluded.

SELL at or below the configured threshold only proposes owned shares minus
already-reserved sell quantity. Empty or fully reserved inventory yields
`NO_TRADE`; short selling is unavailable. Entry regime gates do not prevent
inventory reduction. Inventory and signal freshness checks still apply. A
proposal does not reserve shares: downstream risk/execution must atomically
recheck available inventory, expiry, and reservations to prevent duplicate sells.

BUY at or above its threshold requires a matching known entry-eligible regime.
The stop must be positive and strictly below the entry band. Intermediate
probabilities yield `NO_TRADE`. Model, regime, and policy versions accompany every
decision for audit. Run `python -m pytest tests/test_strategy.py -q` for synthetic
boundary, invalid-input, freshness, calendar, regime, and inventory tests.
