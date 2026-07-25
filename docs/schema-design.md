# Schema design

Three tables in one Postgres database, each owned by exactly one service.

## Ownership

- **chatbot** (`app/`) owns `conversations` + `messages` — transactional
  application data.
- **ingestion** (`ingestion/`) owns `inference_logs` — append-only telemetry.

Both services connect to the same Postgres instance but write different tables,
and neither reads the other's. That discipline is what keeps the split
meaningful: the services share infrastructure today, and could be pulled onto
separate databases tomorrow without changing a query.

**ORM:** SQLModel (Pydantic + SQLAlchemy) — the same mental model as the
Pydantic wire contract, and native to FastAPI.

## The tables

```
conversations                 messages                       inference_logs
-------------                 --------                       --------------
session_id  PK (uuid str)     id           PK (auto)         event_id     PK (uuid str)
created_at                    session_id   FK -> conv        session_id   (indexed, NOT a FK)
updated_at                    role         (user|assistant)  provider
                              content      (full text)       model
                              created_at   (indexed)         status       (indexed)
                                                             error_type / error_message
                                                             started_at (indexed) / ended_at
                                                             latency_ms / ttft_ms
                                                             input_tokens / output_tokens
                                                             input_preview / output_preview
                                                             created_at
```

## Why messages and inference_logs are separate tables

They are different *kinds* of data with different volume, lifecycle, and
scaling needs:

- `messages` is application state a user cares about and expects to persist.
- `inference_logs` is observability — one row per LLM call, **including calls
  that never became a visible message**: errors, cancelled streams, retries.

The cardinalities differ (a single message can correspond to several inference
calls), the retention policies differ (you might aggressively prune telemetry
while keeping conversations indefinitely), and the read patterns differ (point
lookups by session vs. time-range aggregates). Merging them would mean one table
serving two access patterns badly, with observability columns polluting the
conversation model.

## Why `inference_logs.session_id` is not a foreign key

It is a plain indexed column. Telemetry is written by a **different service**,
asynchronously, and may legitimately reference a session that has no
`conversations` row — an error raised before any message was persisted is the
common case. A foreign key would make the telemetry path fail on exactly the
events it most needs to record. Referential integrity is the wrong guarantee for
an observability table.

## Wire model ≠ storage model

`sdk/events.py::InferenceLog` (pure Pydantic, the transport contract) is a
separate type from `db/models.py::InferenceLogRow` (SQLModel, the storage shape).
The mapping between them is trivial today, and that is the point — keeping them
distinct costs almost nothing and means the API contract and the database schema
can evolve independently. It also keeps the SDK free of any database dependency,
so a consumer of the SDK never installs SQLModel.

## Indexes

Chosen for the queries actually run, not speculatively:

| Index | Serves |
|---|---|
| `messages(session_id)` | rebuild a conversation; build the context window |
| `messages(created_at)` | ordering within a session |
| `inference_logs(session_id)` | every call belonging to one conversation |
| `inference_logs(started_at)` | time-range filters and the dashboard's time buckets |
| `inference_logs(status)` | error-rate aggregates |

## Schema creation, and the migration gap

Schema is created by a one-time `python -m db.init`
(`SQLModel.metadata.create_all`), **not** on application startup. Two services
each calling `create_all` on boot race on DDL and one dies with a Postgres
catalog unique-violation — found the hard way. Under Docker Compose a one-shot
`db-init` service performs it and both apps wait on its completion.

`create_all` creates missing tables but never `ALTER`s existing ones, so adding a
column to a live database does nothing — adding `ttft_ms` required a fresh
volume. This is the clearest known limitation in the project; **Alembic** is the
fix and is listed in the README's improvements.

## Testing against SQLite

Tests run against in-memory SQLite via a dependency override, so the suite needs
no Postgres and finishes in seconds. SQLModel makes the same table definitions
work on both.

SQLite is not Postgres, though, and where the two diverge in a way that matters —
the ordered-set aggregates and epoch bucketing behind `/stats` and
`/stats/timeseries` — the code branches on the dialect. Testing only SQLite would
leave the production branch uncovered, so the suite runs against **both**: set
`TEST_DATABASE_URL` to a Postgres URL and the same tests execute there, and CI
runs both legs.

`tests/test_backend_parity.py` holds that arrangement honest. It pins the two
branches to identical answers, and asserts the dialect actually in use matches the
one configured — otherwise a broken CI service container would produce a green
"postgres" run that silently tested SQLite.

## Timestamps: naive UTC, enforced at the column

Every timestamp column is `TIMESTAMP WITHOUT TIME ZONE`, holding UTC. That
convention is load-bearing rather than stylistic, because Postgres converts an
*aware* datetime to the **session** `TimeZone` before discarding the offset — and
the SDK stamps aware UTC timestamps. Against a server running in UTC that stores
correctly; against one running in Asia/Kolkata every row lands +5:30 off, silently,
corrupting the dashboard's time buckets and `since` filters.

Running the suite against a non-UTC Postgres is what surfaced this. The fix is the
`UtcDateTime` type decorator in `db/models.py`, which normalises on bind so the
convention holds for **every** write path — including code that constructs a row
directly instead of going through the ingestion boundary. A Pydantic field
validator would not have covered that, because SQLModel does not run validators on
`table=True` models.

`TIMESTAMPTZ` would make the whole class of bug impossible and is the better end
state. It is deferred because it is a column-type change with no migrations in
place, and because SQLite ignores `timezone=True` — which would reintroduce a
backend divergence in the tests.
