# Paper portfolio ledger

Run the existing Alembic upgrade command to apply migration `0003`. The ledger
stores an append-only event journal in PostgreSQL. Account IDs isolate portfolios;
use stable instrument IDs across symbol changes. This module has no order API.

Construct a `LedgerEvent` with a unique event ID, aware occurrence time, source
(`manual`, `paper`, or `import`) and kind (`deposit`, `withdrawal`, `buy`, `sell`,
or `mark`). Fills include quantity, price, total fee for that fill, and optionally
an order ID. Each partial fill needs its own event ID even when order IDs match.
Use decimal strings, never binary floating-point arithmetic, for money inputs.

Within `engine.begin()`, call `ingest_event(connection, account_id, event)`.
The account row is locked until transaction completion, serializing ingestion
and inventory checks. Identical retries return current state without inserting;
reusing an event ID with changed data fails. An exception should roll back the
caller transaction. `read_ledger` reconstructs state in persisted sequence order.
Database triggers reject updates/deletes to events. Corrections require an
explicit reconciled journal; backdated new events are rejected, not silently
reordered. Manual fills follow exactly the same accounting and validation.

Cash flows, fees, fills and marks persist as JSON with decimal strings. Holdings,
cash, equity and P&L are deterministic projections of that journal. A buy reduces
cash by gross cost plus fees, adding both to weighted-average cost basis. A sale
adds gross proceeds minus sell fees, and realizes those net proceeds less the
allocated basis. Partial sales allocate basis to six decimals using half-even
rounding; the final sale consumes the exact remaining basis. Fees are reported
separately but never subtracted again. Accounting uses a local 60-digit Decimal
context and bounded six-decimal inputs.

Equity equals cash plus marked holdings. Net P&L equals equity minus net external
contributions and must equal realized plus unrealized P&L on every replay. Marks
are explicit events; fills initially mark that instrument at the fill price.
The snapshot time is the last event time, not a guarantee that all marks are
fresh. Risk consumers must obtain suitable current marks. Missing capital,
oversells, negative cash, malformed values and marks for unowned instruments fail.
This is long-only, single-currency accounting without borrowing, corporate-action
entitlements, tax-lot accounting or currency conversion.

Tests cover hand-calculated partial fills, net P&L, fees, manual trades, retries,
conflicts, full liquidation rounding, invalid transitions and PostgreSQL
round-trips/rollback/immutability. No live trading is enabled.
