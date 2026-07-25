# Architecture Notes

A lightweight LLM **inference logging & ingestion** system. Two independent
FastAPI services share one Postgres database; a small SDK layer captures
inference metadata and ships it out of the request path.

```
   React chat UI                ┌────────────────────────────────────────┐
   (frontend/, served at :8000) │  chatbot service  (app/, :8000)        │
   ── POST /chat ──────────────▶│                                        │
   ── POST /chat/stream (SSE) ─▶│  /chat:                                │
   ── GET  /conversations ─────▶│    1. persist user message             │
   ── GET  /conversations/{id} ▶│    2. build context window (last N)    │
                                │    3. traced.chat/.stream(…) ── SDK    │
                                │    4. persist assistant message        │
                                └───────┬────────────────────┬───────────┘
                                        │ writes             │ emit InferenceLog
                                        │ conversations,     │ (non-blocking)
                                        ▼ messages           ▼
                              ┌──────────────┐    ┌────────────────────────┐
                              │  Postgres    │    │ QueueSink (in-process) │
                              │              │    │  background thread     │
                              │ conversations│    └───────────┬────────────┘
                              │ messages     │                │
                              │ inference_   │      ┌─────────┴──────────┐
                              │   logs       │      │                    │
                              └──────┬───────┘   HTTP POST          XADD (Redis
                                     ▲           /logs              Stream)
                                     │              │                    │
                                     │              ▼                    ▼
                                     │   ┌──────────────────┐  ┌──────────────┐
                                     │   │ ingestion (:8001)│  │ Redis Stream │
                                     ├───│  POST /logs      │  └──────┬───────┘
                                     │   │  validate→store  │         │ consumer
   React dashboard ──────────────────┤   └──────────────────┘         │ group
   (dashboard/, served at :8001)     │                                ▼
   ── GET /stats, /logs, …  ─────────┘   ┌────────────────────────────────────┐
                                         │ worker (ingestion/worker.py)       │
                                         │  consume → store → ack             │
                                         └────────────────────────────────────┘
```

Two log transports exist and are selected by configuration: direct HTTP POST by
default, or the Redis Stream + worker path when `REDIS_URL` is set (the Docker
Compose default). Both converge on the same `store_log()`.

## Ingestion flow

1. **Capture.** Every LLM call goes through `TracedClient` (`sdk/tracing.py`).
   It times the call, extracts model / tokens / status from the response (or the
   error), and builds a structured `InferenceLog` (`sdk/events.py`) — a pure
   Pydantic model that is the **wire contract**. Streaming calls are teed, so one
   log is emitted at stream close carrying both TTFT and total latency.
   A monkey-patch alternative (`sdk/instrument.py`) applies the same capture with
   no call-site change at all.
2. **Redact.** Previews are PII-scrubbed **at the source**, before the event is
   emitted — the raw value never leaves the process inside telemetry
   (`sdk/redaction.py`).
3. **Emit, out of the request path.** The log is handed to a `QueueSink`
   (`sdk/sinks.py`), which enqueues and returns immediately. A background thread
   drains the queue into the configured inner sink: `HttpSink` (`POST /logs`) or
   `RedisStreamSink` (`XADD`).
4. **Validate & parse.** Ingestion validates the payload against the same
   `InferenceLog` model — FastAPI does it for `POST /logs` (malformed → `422`);
   the worker does it explicitly via `model_validate_json`.
5. **Store.** Both paths call the shared `store_log()`, which maps the validated
   wire model to `InferenceLogRow` (`db/models.py`) — the **storage model**,
   deliberately a separate type — and inserts into `inference_logs`. The chatbot
   separately persists `conversations` and `messages` in its own request path.
6. **Read.** `GET /conversations` and `/conversations/{id}` serve the chat UI;
   `/stats`, `/stats/timeseries`, `/stats/by_model` and `/logs` serve the
   dashboard, aggregating in the database.

## Logging strategy

- **Capture, schema, and destination are three decoupled concerns.** That is what
  made each capability a swap rather than a rewrite — HTTP shipping, the queue,
  the broker, multi-provider, streaming, and the monkey-patch layer all landed
  without changing `InferenceLog` or the chat code. See
  [docs/sdk-design.md](./docs/sdk-design.md).
- **Two instrumentation mechanisms, one capture path.** The explicit wrapper
  (used by the app, because it also normalizes the response and handles streams)
  and the monkey-patch (`instrument()`, zero-touch) share adapters, event schema,
  redaction, and sink. Only the application mechanism differs.
- **Decoupled from the chat path.** Logging is fire-and-forget: the chatbot never
  waits on log delivery and never fails because of it.
- **Observe, never suppress.** On an LLM error `TracedClient` records
  `status="error"` and **re-raises**, so the caller handles the failure exactly as
  it would without instrumentation.
- **Wire model ≠ storage model,** so the transport contract and the database
  schema evolve independently.
- **App data and telemetry are separate tables** — see
  [docs/schema-design.md](./docs/schema-design.md).

## Failure-handling assumptions

- **Logging must never block or break the chat.** `QueueSink` enqueues and
  returns instantly; delivery happens on a background thread. If ingestion is
  **slow**, the chat is unaffected. If it is **down**, delivery fails, the error
  is swallowed and counted, and the chat still returns `200`. Verified by test
  and end-to-end with ingestion stopped.
- **A full queue drops rather than blocks.** Losing telemetry is acceptable;
  stalling a user's request is not. Drops and delivery failures are counted on
  the sink (`dropped` / `failed`) so the loss is at least visible.
- **Telemetry is decoupled from app data.** `inference_logs.session_id` is a
  plain indexed column, **not** a foreign key — a log may reference a session
  with no `conversations` row (an error before any message was stored).
- **Durable transport when `REDIS_URL` is set.** Logs `XADD` to a Redis Stream; a
  worker consumes with a consumer group and **acks only after storing**
  (at-least-once). If the consumer or the database is down, logs wait durably in
  the stream and replay on recovery — verified by stopping the worker, chatting,
  and watching the backlog persist when it returns. Duplicates on replay are
  bounded by the `event_id` primary key. Poison messages (unparseable payloads)
  are dropped after logging rather than redelivered forever.
- **Cancellation is a recorded outcome, not a gap.** A stream aborted by the
  client emits `status="cancelled"` with the partial output and TTFT.
- **Remaining window, accepted:** events sitting in the in-process `QueueSink`
  when the *chatbot itself* crashes before `XADD` are lost. Closing it means a
  synchronous durable write on the request path, which would violate the first
  assumption above.

## Scaling considerations

- **Independent services.** Chatbot and ingestion are separate FastAPI apps and
  scale independently — ingestion can be replicated behind a load balancer to
  absorb log volume without touching the chat tier.
- **The broker is the pressure valve.** Bursts absorb into the Redis Stream and
  drain at the worker's pace; the consumer group means you scale by running more
  worker replicas, which share the stream and ack their own entries. The SDK sink
  kept its shape — only the destination changed.
- **Aggregate in the database, not the app.** Percentiles (`percentile_disc` as
  an ordered-set aggregate) and time buckets (floored-epoch `GROUP BY`) execute
  in Postgres, so a large window returns a few rows instead of the whole table.
  The Python fallback exists only for SQLite in tests.
- **Bounded reads.** `GET /logs` caps page size, so no single request can make the
  service materialise the table.
- **Database.** Indexes follow the actual read paths: `messages(session_id)`,
  `inference_logs(session_id)`, `inference_logs(started_at)`,
  `inference_logs(status)`. At high telemetry volume `inference_logs` is the
  table to watch — the natural moves are time-based partitioning plus
  retention/archival, and for heavy dashboard aggregation a columnar store (e.g.
  ClickHouse). That is how production LLM-observability tools (Langfuse,
  Helicone) split the OLTP and OLAP read paths.
- **Batching.** The SDK ships one event per call; batching enqueued events before
  delivery is a cheap throughput win under load.

## Deeper detail

Per-component design rationale lives in [docs/](./docs/) — SDK, schema, read API,
auto-instrumentation, streaming, event architecture, PII redaction, dashboard.
