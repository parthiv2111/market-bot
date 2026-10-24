# Market Bot

Python research and paper-trading project. Implementation roadmap: issue #1, starting with PB-01 in issue #2.

Live trading and broker order placement remain disabled. No returns are guaranteed.

Development uses a separate branch per issue and merges only after the acceptance criteria and validation pass. Work resumes from committed checkpoints.

## Architecture

- [Python modular monolith ADR](docs/adr/0001-python-modular-monolith.md)
- [MVP scope and execution modes](docs/mvp-scope.md)
- [PB-01 acceptance cases and validation](docs/testing/pb-01.md)

## Local development

Use Python 3.12 and uv. The project deliberately restricts the Python minor
version so the numerical and web stack share one tested runtime. `uv.lock`
pins the complete dependency graph; commit intentional lock updates together
with dependency changes. No market data, broker account or database is required
to run the bootstrap checks.

```console
uv sync --locked --python 3.12
uv run --locked prediction-bot health
uv run --locked pytest
uv run --locked ruff check .
uv run --locked mypy
uv run --locked uvicorn prediction_bot.api.app:create_app --factory
```

The first sync can download Python and packages; subsequent tests run offline.
Visit `http://127.0.0.1:8000/health` after starting the API. This endpoint reports
process health, not database readiness or permission to trade. The only CLI
commands are `health` and `validate-config PATH`. Live orders are unavailable, regardless of environment
variables. `.env.example` documents future settings; it is not loaded yet.

Keep local secrets in ignored `.env` files and datasets in `data/` or `datasets/`.
Generated artifacts belong in `artifacts/`. Never commit credentials or private
data. Windows PowerShell and Unix shells use the same uv commands above.

To verify a fresh installation, remove/recreate only your disposable virtual
environment or set `UV_PROJECT_ENVIRONMENT` to a new empty directory and repeat
the locked sync and checks. Runtime PostgreSQL setup and validated configuration
are delivered in their respective backlog issues.

See [CI checks and maintainer setup](docs/testing/ci.md) for required status checks,
offline-test policy and deliberate-failure verification.

See [validated settings and immutable policies](docs/settings.md). Validate the
research example with `uv run --locked prediction-bot validate-config config/research.example.json`.

See [PostgreSQL setup and migrations](docs/database.md) for local services and
isolated integration tests.

See [data sources and access constraints](docs/data-sources.md) before implementing
providers. NSE website automation is not approved; use authorized local files
and synthetic fixtures while source eligibility and coverage remain unresolved.

[Instrument identity and supplied calendars](docs/reference-data.md) provide
versioned aliases and explicit Asia/Kolkata sessions without inventing missing history.

The [daily provider interface and CSV importer](docs/providers.md) load authorized
local files with provenance, raw/adjusted prices and explicit coverage metadata.

[NSE UDiFF report import](docs/nse-reports.md) supports authorized local cash-market
reports and preserves the original file, source date, series and parser version.

An [optional yfinance research adapter](docs/yahoo-provider.md) supports explicit
NSE mapping, adjustment semantics and caching; it is disabled by default.
