# Design docs

Rationale behind the non-obvious decisions. Start with
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) for the system overview — these go one
level deeper on individual pieces.

| Doc | What it covers |
|---|---|
| [sdk-design.md](./sdk-design.md) | The instrumentation layer: three decoupled concerns (schema / capture / sink), the provider adapters, and why the wrapper observes but never suppresses |
| [schema-design.md](./schema-design.md) | The three tables, why telemetry is separate from messages, why `session_id` is not a foreign key, index choices, and the migration gap |
| [read-api.md](./read-api.md) | List / resume / metrics endpoints: avoiding N+1, aggregating in the database, and bounded pagination |
| [auto-instrumentation.md](./auto-instrumentation.md) | The monkey-patch layer — zero-touch capture, and how it differs from the wrapper |
| [streaming.md](./streaming.md) | Teeing the token stream, capturing TTFT, and treating cancellation as a first-class outcome |
| [event-architecture.md](./event-architecture.md) | The Redis Stream + worker path: what durability gap it closes, at-least-once delivery, poison messages |
| [pii-redaction.md](./pii-redaction.md) | Regex detectors, typed-token replacement, redacting at the source, and the honest limits |
| [dashboard.md](./dashboard.md) | The observability console: two planes (overview + log explorer) and why it lives on the ingestion service |

`screenshots/` holds the images used by the top-level README.
