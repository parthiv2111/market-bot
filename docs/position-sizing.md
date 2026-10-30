# Paper position sizing

`sizing.size_entry` consumes a BUY proposal, current `SessionState`, reconciled
`PortfolioCapacity`, `EntryTerms`, explicit `SizingPolicy`, and aware current time.
It returns SIZED or NO_TRADE. SIZED is a capacity estimate, not permission to send
an order. Remaining entry-evidence, market-status, stress and atomic reservation
gates must pass before dispatch. Live order placement remains disabled.

Policy percentages use fixed session E0. All fields are required research inputs,
not validated guarantees. `remaining_risk_budget` is total account headroom before
existing/pending heat, after applying heat-cap, loss-budget, period-stop and floor
constraints. The function subtracts current-to-stop heat plus each exposure's
`additional_exit_risk` (total conservative remaining costs/gap allowance). It then
takes the minimum of that headroom and per-trade risk. Intraday winnings must not
increase the supplied account headroom.

Supply every held position and pending BUY across strategies. Pending marks must
be conservative maximum fill prices and reserved cash must include purchase
charges; cancellation requests remain pending until confirmed. Held marks use
current conservative values. Symbol/sector/gross capacity uses total notional;
overlapping correlation groups each consume their own capacity. Position count
counts distinct held or pending symbols. Explicit classifications are mandatory.
The caller must supply a fresh liquidity quantity cap from completed observations.

Candidate risk is quantity times entry-minus-stop plus per-share gap/slippage,
plus buy and sell charges. Each side's charge is the larger of its per-share
charge and minimum charge. All-in purchase cash includes buy charges. A monotonic
binary search finds the largest permitted whole-lot quantity under every capacity
and recomputes exact costs. Other fee schedules need a conservative envelope in
this supported model; arbitrary nonmonotonic tariffs are not supported.

The plan example uses E0 1,000,000, 0.25% risk, entry 1,000, stop 980 and combined
cost/gap allowance 5 per share: budget 2,500 / risk 25 = 100 shares. A 10% symbol
cap also permits 100 shares. Stops are never altered. Additions below average
entry, additions to currently losing positions, and lower replacement stops are
rejected. Insufficient unreserved cash prohibits borrowing. If one lot exceeds
any capacity, return NO_TRADE; never round up.

Session/portfolio/liquidity freshness, proposal window and entry band are checked.
Stop latches and unreconciled account state block sizing. Re-evaluate after fills
or price changes and reserve atomically before submission: this pure function
alone cannot prevent concurrent workers consuming the same capacity.
