# Architecture Notes

A lightweight LLM **inference logging & ingestion** system. Two independent
FastAPI services share one Postgres database; a small SDK wrapper
auto-captures inference metadata and ships it out of the request path.

```
                          ┌──────────────────────────────────────┐
   browser (plain HTML/JS)│  chatbot service  (app/, :8000)        │
   ── POST /chat ────────▶│                                        │
   ── GET  /conversations │  /chat:                                │
   ── GET  /conversations/│    1. persist user message             │
        {id}   ───────────▶│    2. build context window (last N)   │
                          │    3. traced.chat(...) ── SDK wrapper  │
                          │    4. persist assistant message        │
                          │                                        │
                          │  reads: list / resume conversations    │
                          └───────┬───────────────────┬────────────┘
                                  │ writes            │ enqueue InferenceLog
                                  │ conversations,    │ (non-blocking)
                                  ▼ messages          ▼
                        ┌──────────────┐    ┌───────────────────────┐
                        │  Postgres    │    │ QueueSink (in-process) │
                        │              │    │  background worker     │
                        │ conversations│    └───────────┬───────────┘
                        │ messages     │                │ HTTP POST /logs
                        │ inference_   │                ▼
                        │   logs  ◀────┼────┐  ┌────────────────────────┐
                        └──────────────┘    └──│ ingestion service      │
                             ▲                  │ (ingestion/, :8001)    │
                             │ GET /stats       │  POST /logs: validate  │
                             └──────────────────│  → store InferenceLog  │
                                                └────────────────────────┘
```

## Ingestion flow

1. **Capture.** Every LLM call in the chatbot goes through `TracedClient`
   (`sdk/tracing.py`). It times the call, extracts model / tokens / status from
   the response (or error), and builds a structured `InferenceLog`
   (`sdk/events.py`) — a pure Pydantic model that is the **wire contract**.
2. **Emit (out of the request path).** The log is handed to a `QueueSink`
   (`sdk/sinks.py`), which puts it on an in-process queue and returns
   immediately. A background worker thread pulls from the queue and delivers via
   `HttpSink` — an HTTP `POST /logs` to the ingestion service.
3. **Validate & parse.** Ingestion (`ingestion/main.py`) receives the JSON and
   FastAPI validates it against the same `InferenceLog` model (malformed → `422`
   before any of our code runs).
4. **Store.** The validated wire model is mapped to `InferenceLogRow`
   (`db/models.py`) — the **storage model**, deliberately separate — and inserted
   into `inference_logs`. The chatbot separately persists `conversations` and
   `messages` in its own request path.
5. **Read.** `GET /conversations` and `GET /conversations/{id}` (chatbot) serve
   the UI; `GET /stats` (ingestion) aggregates latency / throughput / errors over
   `inference_logs` — the seed of a dashboard.

## Logging strategy

- **Auto-instrumentation via a transparent wrapper.** `TracedClient.chat(...)`
  mirrors the raw provider SDK and returns the raw response, so chat code calls
  it as a drop-in and carries **zero logging concerns**. Capture, schema, and
  destination are three decoupled concerns (see `sdk/DESIGN.md`), which is why
  each evolution — HTTP shipping, the queue, a future monkey-patch
  auto-instrument, multi-provider — is a *swap*, not a rewrite.
- **Decoupled from the chat path.** Logging is fire-and-forget: the chatbot
  never waits on log delivery and never fails because of it (see failure
  handling below).
- **Wire model ≠ storage model.** `InferenceLog` (transport) and
  `InferenceLogRow` (database) are separate types so the contract and the schema
  can evolve independently.
- **Separate tables for app data vs telemetry.** `messages` (transactional, a
  user's conversation) and `inference_logs` (append-only observability, one row
  per call incl. retries/errors that never became a visible message) live apart,
  so each can scale, be pruned, and evolve independently.

## Failure-handling assumptions

- **Logging must never block or break the chat.** `QueueSink.emit` enqueues and
  returns instantly; delivery happens on a background thread. If ingestion is
  **slow**, the chat is unaffected. If ingestion is **down**, delivery fails and
  the error is **swallowed and logged as a warning** — the chat still returns
  `200`. (Verified by test and end-to-end: with ingestion stopped, `/chat` keeps
  working.)
- **`TracedClient` observes, never suppresses.** On an LLM error it records an
  `status="error"` log and then **re-raises**, so the caller / FastAPI still
  handles the failure normally.
- **Telemetry is decoupled from app data.** `inference_logs.session_id` is a
  plain indexed column, **not** a foreign key — a log may reference a session
  that has no `conversations` row (e.g. an error before any message was stored).
- **Known gap (accepted tradeoff):** the queue is **in-process**. A chatbot
  crash loses any events still queued, and there is no retry/back-off or
  dead-letter. This is the deliberate boundary of the current rung; durability is
  what the *event-based architecture* bonus (an external broker) would add.

## Scaling considerations

- **Independent services.** Chatbot and ingestion are separate FastAPI apps and
  scale independently — ingestion can be replicated behind a load balancer to
  absorb log volume without touching the chat tier.
- **The queue is the pressure valve, and the current bottleneck.** It absorbs
  bursts today but is single-process and non-durable. The first real scaling step
  is replacing it with a durable broker (Kafka / Redis / SQS) plus stateless
  ingestion workers — the SDK sink stays the same shape, only its destination
  changes.
- **Database.** Indexes are chosen for the actual read paths:
  `messages(session_id)` (rebuild a conversation / context window),
  `inference_logs(session_id)` (a conversation's calls),
  `inference_logs(started_at)` (time-series/throughput), `inference_logs(status)`
  (error rate). At high telemetry volume, `inference_logs` is the table to watch:
  the natural moves are time-based partitioning + retention/archival, and — for
  heavy dashboard aggregation — a columnar analytics store (e.g. ClickHouse),
  which is how production LLM-observability tools (Langfuse, Helicone) split the
  OLTP and OLAP read paths.
- **Batching.** The SDK ships one event per call today; batching enqueued events
  before POSTing is a cheap throughput win under load.

## What would come next (see README "what I'd improve")

Alembic migrations · external durable queue · monkey-patch auto-instrument ·
multi-provider · streaming (+ cancel) · dashboard UI over `/stats` · output
sanitization (DOMPurify) · Docker Compose one-command setup.
