# ADR 0001: Python modular monolith

Status: accepted for research implementation; live execution is disabled.

Tracks [PB-01 / issue #2](https://github.com/parthiv2111/market-bot/issues/2).
The [roadmap and risk specification](https://github.com/parthiv2111/market-bot/issues/1)
govern subsequent implementation.

## Context and decision

Build one Python package with explicit domain boundaries, not independently
deployed microservices. Python CLI jobs orchestrate ingestion, dataset creation,
training and replay. Jobs record run IDs, input versions and checkpoints so a
retry is reproducible and cannot duplicate durable effects. Defer Redis/Celery
until measured queue throughput or independent worker scaling justifies their
operational cost. A local scheduler can invoke the same CLI entry points.

Use FastAPI for typed HTTP interfaces, initially read-only research and risk
queries. Use Streamlit for the dashboard; do not introduce a JavaScript frontend.
Neither transport owns business rules or accesses a broker directly.

Use pandas/NumPy for tabular features and numerical operations, scikit-learn for
baseline models, train-only preprocessing and calibration. Persist data snapshots,
split boundaries, random seeds and model versions for reproducible evaluation.
Chronological splits must purge labels that overlap evaluation windows.

Use PostgreSQL as the durable source of truth, SQLAlchemy for persistence and
Alembic for versioned schema migrations. Risk reservations, order intents and
stop latches require transactional writes shared across all callers. SQLite is
not evidence of PostgreSQL concurrency correctness. Raw imports and model files
may reside in local artifact storage, referenced by immutable hashes in the DB.

## Module ownership

These are proposed package boundaries, not a claim that they exist yet. Each
module's implementer owns its contracts and tests; maintainers review changes.

| Module | Owns | Boundary |
| --- | --- | --- |
| settings | Validated configuration and policy versions | No secrets in source control |
| data | Providers, instrument identity, sessions, validation, corporate actions | Immutable provenance; no strategy decisions |
| features | Deterministic indicators and regimes | Only information available at event time |
| research | Labels, snapshots, splits, fitting and calibration | No order placement |
| strategy | Versioned proposals and NO_TRADE reasons | Cannot override risk rejection |
| portfolio_accounting | Cash, fills, equity, costs and external flows | Authoritative account valuation |
| risk_engine | Sizing, exposure, reservations, persisted stops and profit floors | Independent of prediction model |
| execution | Durable order lifecycle, exits and reconciliation | Dispatch rechecks risk; timeouts are unknown outcomes |
| broker_adapters | Paper simulation and future broker protocol mapping | Live adapter unavailable in MVP |
| backtest | Deterministic event replay and cost/fill assumptions | Reuses accounting and risk rules |
| persistence | SQLAlchemy repositories and Alembic migrations | Transaction and idempotency boundaries |
| api | FastAPI transport and later authenticated operations | Calls application services |
| dashboard | Streamlit views and explanations | Calls API; cannot submit broker orders |
| jobs | CLI orchestration, checkpoints and run metadata | Calls services, not duplicated domain rules |
| monitoring | Health, audit visibility and incident reporting | Never treats unknown exposure as flat |

Dependency direction: API/dashboard/jobs -> application services -> domain rules
and provider/repository interfaces. Infrastructure implements those interfaces.
Domain risk rules must not import UI, HTTP or broker SDK implementations.

Validated events -> strategy proposal -> risk gate and atomic reservation ->
execution manager -> paper broker. Fills and holdings -> reconciliation ->
accounting -> risk state. Risk exits have priority over entry opportunities.
All transitions produce durable audit records with event time, input freshness,
model/policy version, reason codes and relevant order identifiers.

## Consequences

One deployable package keeps local operation and debugging straightforward, but
module boundaries must be enforced through reviews and tests. PostgreSQL adds a
local service dependency in return for durable concurrent risk decisions. Batch
research jobs must not block a future event-driven risk monitor. Extract workers
only after measuring this contention; do not solve it with duplicated risk state.

## Validation

See [MVP scope and modes](../mvp-scope.md) and the
[PB-01 acceptance cases](../testing/pb-01.md). Package bootstrapping and executable
service implementation belong to PB-02 and later issues.
