# PostgreSQL development and migration checks

PostgreSQL 18 is the development/test baseline. SQLite is not used as a substitute
for PostgreSQL constraints or migration evidence. Imports never open connections.

Set `PREDICTION_BOT_DB_PASSWORD` locally, then run `docker compose up -d postgres`.
The persistent app database binds only to localhost:5432. Check readiness with
`docker compose exec postgres pg_isready -U prediction_bot -d prediction_bot`.
Set `PREDICTION_BOT_DATABASE_URL` to the corresponding
`postgresql+psycopg://USER:PASSWORD@localhost:5432/prediction_bot` URL, then run:

```console
uv run --locked alembic upgrade head
uv run --locked alembic current
```

Do not commit passwords or URLs with credentials. Shell environment variables
are read explicitly; `.env.example` is documentation and is not loaded by Python.
Database errors should not be logged with connection URLs. SQL bind parameters
are hidden by the engine; callers must also avoid logging raw input records.

The test service is separate, ephemeral and bound to localhost:55432:

```console
docker compose --profile test up -d postgres-test
```

Set `PREDICTION_BOT_TEST_DATABASE_URL` to
`postgresql+psycopg://prediction_bot:local-test-only@localhost:55432/prediction_bot_test`.
Run `uv run --locked pytest -m postgres`. Tests require the `_test` database suffix,
create a uniquely named disposable schema, migrate it and remove only that schema
on completion. They never migrate or drop the application's public schema.
Without the test URL, these integration tests explicitly skip; CI supplies a
PostgreSQL service and runs them separately. Ordinary offline tests need no DB.

Migration 0001 creates instruments, provider-specific daily candles, ingestion
runs and policy history. Migration 0002 makes policy updates/deletes fail with
a PostgreSQL trigger. Tests exercise empty-schema creation and preservation of
existing records during upgrade, reapplying head, ORM/migration agreement,
foreign keys, OHLCV constraints and symbol/session/provider uniqueness.

Events are stored as TIMESTAMP WITH TIME ZONE. Engine sessions use UTC; the Python
type rejects naive datetimes and normalizes aware values to UTC. The exchange's
session date remains a Date rather than an event instant. Raw SQL clients must
supply explicit timestamp offsets: PostgreSQL cannot infer whether a client
originally supplied a naive value after conversion to timestamptz.

Versioned policies remain append-only for application operations. A database
owner can still alter schema/triggers; use restricted application roles in a
deployment. The local file registry remains supported; migrating existing policy
files into PostgreSQL and future account/order tables are separate work.

Back up before schema changes. Downgrades are development-only and may destroy
data or remove immutability protections; never run them against production as an
automatic rollback. No trading execution is started by migrations or health checks.
