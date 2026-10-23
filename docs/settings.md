# Configuration and policy versions

Load a JSON file explicitly with `load_settings(Path(...))`; module imports do
not load secrets or start jobs. [Research example](../config/research.example.json)
contains illustrative values, not live authorization. Mode defaults to paper,
provider to local_csv, and strategy to daily_five_session. Live modes, unknown
providers/strategies and exception activation are rejected.

Every percentage is required, finite, strictly positive and at most 100 percentage
points (`1.0` means 1%). Numeric strings and booleans are rejected. Per-trade risk
must not exceed portfolio heat, heat must not exceed the daily loss limit, and
daily/weekly/monthly limits must be nondecreasing. These structural bounds are
not approved live limits. Missing risk settings never silently load defaults.

Use `PolicyStore(Path("artifacts/policies"))` to save a validated Settings object.
The SHA-256 version identifies canonical public configuration bytes; secret
fields are excluded. Publishing uses flushed temporary files and exclusive hard
links, preventing concurrent writers from overwriting a record. Existing damaged
records fail integrity checks. This requires a local filesystem supporting hard
links (tested on Windows; CI also tests Linux). Filesystem permissions and backups
must protect the directory; this is not a tamper-proof external audit service.

`SettingsManager.reload(path)` validates and persists a pending version. It never
accesses queues, counters, stops or broker state. `bind_new_session(session_id)`
persists an immutable binding, returning the existing binding if that session
already exists, including after restart. Queued intents must carry the bound
version and resolve it at dispatch; they must not consult the pending version.
Future session and execution services must enforce calendar eligibility and
persist their own operational state. Reload provides no operational reset API.

Secrets use SecretStr, are excluded from serialization/repr, and are absent from
policy files. Use `load_settings` at input boundaries: it converts parser errors
to sanitized ConfigurationError messages without raw input or chained tracebacks.
Never log raw configuration dictionaries, secret getter values or Pydantic
`errors()` output. Stored policies intentionally cannot reconstruct credentials;
inject credentials separately at the eventual provider boundary.

The CLI `validate-config PATH` checks a file without printing its contents.
The health stub reports paper mode but remains a process-health endpoint; it does
not load policies or claim readiness to execute orders.
