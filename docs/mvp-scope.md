# MVP scope and execution boundaries

## Research scope

The MVP covers NSE cash-equity, cash-funded long-only research using daily data
and a five-session (five-day) prediction horizon. SELL reduces owned inventory;
it never opens a short. No leverage, derivatives or intraday strategy is included.
Exchange sessions use Asia/Kolkata, not server-midnight boundaries.

Start with local CSV imports and approved NSE end-of-day reports; yfinance is an
optional research adapter subject to source approval in PB-06. No data source is
declared approved by this architecture. Preserve source, timestamp, adjustment
policy and point-in-time universe membership. Synthetic fixtures keep tests offline.

Daily candles cannot establish intraday event ordering or validate continuous
loss/profit-floor enforcement. Replay must disclose adverse ordering assumptions,
gaps, blocked exits and costs. Five-session probabilities cannot be repurposed as
intraday target-before-stop probabilities. Intraday work needs separate data,
labels, execution assumptions and validation.

## Modes

| Mode | Inputs and output | Order capability |
| --- | --- | --- |
| research | Historical snapshots, models, reports and replay | No broker orders |
| paper | Validated events drive a deterministic simulated broker and ledger | Simulated orders only; no live credentials |
| shadow | Observe authorized market/account data and record hypothetical decisions | Read-only observation; no real order submission |
| disabled-live | Reserved future mode, rejected by MVP startup/configuration | No live adapter activation or orders |

Implementers must reject unknown modes and live activation, rather than silently
falling back. Mode selection alone must never unlock live order placement.
Paper and shadow ledgers are isolated from any real account state. Missing or
stale market/account/session inputs block new exposure; existing protection and
reconciliation continue according to the applicable incident procedure.

## Risk ownership and release boundaries

The independent risk_engine owns approval. A model emits proposals, never an
authorization to trade. Account equity includes open positions, costs and external
cash flows; stop latches, reservations and counters persist across restarts.
The managed account and capital allocation must be explicit; outside holdings
are not claimed to be protected. Risk-reducing exits remain eligible during entry
pauses, but unknown holdings must be reconciled before potentially duplicate sells.

The roadmap's 0.25% planned trade risk, 0.75% heat and 1% daily loss trigger are
illustrative research settings. A 5% daily gain is a soft profit-protection
threshold, not a quota, forecast or expected return. There is no profit guarantee.
Stops and attempted liquidation cannot guarantee a loss ceiling, especially with
gaps, halts or unavailable liquidity. The optional post-5% exception stays OFF.

Promotion proceeds through research, replay, sandbox where available, forward
paper and shadow evidence. Live readiness additionally requires chosen broker
and data eligibility, fault drills, reconciled accounting, validated risk controls,
actual observation periods and explicit separate owner approval of capital risk.
Completing issues or passing unit tests never constitutes live approval.

## Delivery order

PB-01 records architecture; PB-02 creates the package and development environment;
PB-03 adds CI. Follow linked prerequisites for the remaining backlog. Establish
accounting, independent risk and simulated execution before advanced ML or live
integration. There is no runnable trading application in PB-01.
