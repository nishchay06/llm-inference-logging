# LLM inference logging & ingestion

A chatbot whose every model call is **auto-instrumented** — captured by patching
the provider SDK, so application code carries no logging concerns — then shipped
off the request path and persisted for observability, with a console to inspect it.

**Stack:** FastAPI · Anthropic + Gemini · Postgres + SQLModel · Redis Streams ·
React + TypeScript · Docker Compose

[`ARCHITECTURE.md`](./ARCHITECTURE.md) — ingestion flow, logging strategy, scaling,
failure handling · [`docs/`](./docs/) — design rationale per component

[Quick start](#quick-start) · [Manual setup](#manual-setup) ·
[Configuration](#configuration) · [Architecture](#architecture-overview) ·
[Endpoints](#endpoints) · [Schema](#schema-design-decisions) · [Tests](#tests) ·
[Demo](#demo) · [Tradeoffs](#tradeoffs) · [Improvements](#what-id-improve-with-more-time)

## Quick start

```bash
cp .env.example .env        # paste your ANTHROPIC_API_KEY
docker compose up --build
```

Chat at **localhost:8000**, dashboard at **localhost:8001**. This starts Postgres,
Redis, both services and the broker worker; a one-shot `db-init` creates the schema
before either app boots. `docker compose down -v` also drops the volume.

Keys: [Anthropic](https://console.anthropic.com/) and/or
[Gemini](https://aistudio.google.com/apikey). Configure both and the UI offers a
per-request provider switch.

## Manual setup

Python 3.11+ and a running Postgres.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

createdb chatbot && python -m db.init              # one-time schema

uvicorn ingestion.main:app --port 8001 --reload    # terminal 1
uvicorn app.main:app       --port 8000 --reload    # terminal 2
```

`DATABASE_URL` defaults to `postgresql+psycopg://<os-user>@localhost:5432/chatbot`.
Without `REDIS_URL` the SDK ships logs over HTTP to `INGESTION_URL`; set it and run
`python -m ingestion.worker` to exercise the event-based path.

The UIs are Vite builds, not server-rendered pages — Docker builds them, otherwise
run `npm run build` once. Without a build the APIs are unaffected and `/` returns a
503 saying how to build. For UI work, `npm run dev` in `frontend/` (:5173) or
`dashboard/` (:5174) proxies to the backend.

### Configuration

| Variable | Default | Effect |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | Default provider; only ones with a key are offered |
| `LLM_MODEL` | per provider | Override the model |
| `LLM_INSTRUMENTATION` | `patch` | `patch` auto-instruments by monkey-patching the SDK; `wrapper` uses the explicit `TracedClient` |
| `REDIS_URL` | unset | When set, logs go through a durable Redis Stream |
| `DATABASE_URL` | local Postgres | Shared by both services |

Both instrumentation modes record identical `InferenceLog` fields — asserted by
parity tests and confirmed live — so `wrapper` is a rollback, not a behavioural
switch. Conversation history is provider-agnostic, so Gemini can continue a chat
Claude started.

## Architecture overview

Two independent FastAPI services share one Postgres database:

- **chatbot** (`app/`, :8000) — chat UI, `POST /chat` and `/chat/stream`; owns
  `conversations` + `messages`.
- **ingestion** (`ingestion/`, :8001) — receives, validates and stores inference
  logs; owns `inference_logs` and serves the dashboard over them.

```
browser ─▶ chatbot :8000 ──(patched SDK → QueueSink, off the request path)───┐
   ▲          │ writes conversations, messages                              │
   │          ▼                                                    HTTP POST │ or XADD
   │       Postgres ◀── writes inference_logs ── ingestion :8001 ◀───────────┘
   └── UI ─────┘                                     ▲                  (worker drains
                                                     └── dashboard :8001  the stream)
```

Capture happens in `sdk/`: a patched provider client builds a structured
`InferenceLog`, redacts PII from the previews, and hands it to a `QueueSink` —
enqueued instantly and delivered on a background thread, so log shipping can be
slow or fail **without ever blocking or breaking the chat**. Delivery goes either
straight to ingestion over HTTP, or through a **Redis Stream** consumed by a
separate worker, which survives an ingestion or database outage.

What it does:

- **Multi-turn chat** with a rolling context window, persisted so it survives restarts.
- **Auto-instrumentation by default** — both call shapes patched on both providers
  (`messages.create`/`messages.stream`, `generate_content`/`generate_content_stream`),
  capturing model, provider, latency, TTFT, tokens, status, timestamps, session ID
  and previews. Nothing in the app times a call or touches a sink.
- **Multi-provider** — Anthropic and Gemini behind adapters, switchable per request.
- **Streaming with cancel** — SSE; a cancelled stream keeps the partial reply and
  records `status="cancelled"` with TTFT.
- **PII redaction at the source** — emails, phones, Luhn-valid cards, SSNs, IPs and
  API keys become typed tokens before a log is emitted.
- **Dashboard** — KPIs, throughput and latency charts, per-model breakdown, and a
  filterable log explorer where one filter bar scopes charts and stream alike.

## Endpoints

| Method & path | Service | Purpose |
|---|---|---|
| `GET /` | chatbot | Chat UI |
| `GET /providers` | chatbot | Configured providers + default, for the UI selector |
| `POST /chat` | chatbot | Send a message; returns reply + `session_id` |
| `POST /chat/stream` | chatbot | Same, as SSE (`start` / `delta` / `done` / `error`) |
| `GET /conversations` | chatbot | List conversations, newest-active first |
| `GET /conversations/{id}` | chatbot | Full message history (resume) |
| `POST /logs` | ingestion | Receive, validate and store an inference log |
| `GET /stats` | ingestion | Calls, error rate, avg + p50/p95/p99 latency, tokens |
| `GET /stats/timeseries` | ingestion | Per-bucket calls / errors / avg latency |
| `GET /stats/by_model` | ingestion | Per-model calls, error rate, avg latency |
| `GET /logs` | ingestion | Query the log stream (filtered, paginated) |
| `GET /` and `/dashboard` | ingestion | Observability console |

Ingestion's read endpoints share the filters `status`, `provider`, `model`,
`session_id`, `q`, `since`, so one filter bar scopes every panel.

## Schema design decisions

Three tables, each owned by exactly one service — the chatbot owns `conversations`
and `messages`, ingestion owns `inference_logs`. Both use the same Postgres, and
neither reads the other's tables.

**Messages and inference logs are separate tables on purpose.** A conversation is
transactional state a user cares about; an inference log is append-only telemetry —
one row per call, *including calls that never became a visible message* (errors,
cancelled streams, retries). Cardinality, retention and read patterns all differ,
so merging them would mean one table serving two access patterns badly.

**`inference_logs.session_id` is indexed, not a foreign key.** Telemetry is written
by a different service, asynchronously, and may reference a session with no
`conversations` row — an error raised before any message was stored is the common
case. A foreign key would fail on exactly the events most worth recording.

**Wire model ≠ storage model.** `sdk/events.py::InferenceLog` (Pydantic, the
transport contract) is a distinct type from `db/models.py::InferenceLogRow`
(SQLModel), so the API contract and schema evolve independently — and the SDK
carries no database dependency.

**Indexes follow the queries actually run:** `messages(session_id)`,
`inference_logs(session_id)`, `inference_logs(started_at)` for time ranges and
buckets, `inference_logs(status)` for error rates.

**Schema creation is a one-time `python -m db.init`, not a startup hook** — two
services racing to `CREATE TABLE` deadlock on DDL. Full rationale in
[`docs/schema-design.md`](./docs/schema-design.md).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

136 tests in ~3 seconds, no network and no Postgres needed: in-memory SQLite via a
dependency override, with fake provider clients and sinks. The code is testable
because the client, sink and session are all injected.

The same suite runs against a **real Postgres**, since `/stats` and
`/stats/timeseries` branch on SQL dialect and SQLite alone would leave the
production path untested:

```bash
createdb chatbot_test
TEST_DATABASE_URL=postgresql+psycopg://$USER@localhost:5432/chatbot_test python -m pytest -q
```

CI runs both legs and gives the Postgres service a **non-UTC** timezone on purpose.
`tests/test_backend_parity.py` pins both backends to identical answers and asserts
the dialect in use is the one configured, so a broken service container cannot
produce a green run that tested nothing.

## Demo

**Chat** — replies render as markdown.

![Chat reply rendered as markdown with headings, bold text and lists](docs/screenshots/chat.png)

**Streaming and cancel** — tokens arrive live; Cancel replaces Send while a reply
is in flight, and the partial reply is kept.

![A reply streaming in mid-sentence with the Cancel button replacing Send](docs/screenshots/streaming.png)

**Conversations** — past chats list in the sidebar with message counts; selecting
one resumes it.

<img src="docs/screenshots/conversations.png" alt="Sidebar listing past conversations with message counts" width="260">

**Dashboard** — KPIs, throughput and latency. The error rate reads
`0 errors · 2 cancelled`: a user pressing Cancel is not a service failure.

![Dashboard KPI cards with throughput and latency charts](docs/screenshots/dashboard-overview.png)

**Filters** scope the charts and the log stream together.

![Dashboard filtered to successful Anthropic calls in the last 24 hours](docs/screenshots/dashboard-filters.png)

**By model** — the multi-provider mix, with per-model error rate and latency.

![Per-model table showing calls, error rate and average latency](docs/screenshots/dashboard-by-model.png)

## Tradeoffs

Deliberate decisions, and what each costs:

- **Telemetry is dropped rather than allowed to block.** A full `QueueSink` queue
  discards events and delivery failures are swallowed — losing observability is
  acceptable, stalling a user's request is not. Both losses are counted on the sink.
- **A durability window remains at the producer.** With `REDIS_URL`, an ingestion or
  database outage no longer loses logs. Events still in the in-process queue when
  the *chatbot* crashes are lost; closing that needs a synchronous durable write on
  the request path, which contradicts the point above.
- **Cancellations are a third outcome, not failures.** They don't raise the error
  rate, and their duration — which runs until the generator is finalized, not until
  the user left — is excluded from latency aggregates. Errors stay in, because a
  timeout is a real latency observation.
- **Logs store previews, not full payloads.** ~200 characters, PII-redacted, plus
  `error_type`/`error_message`. A storage and privacy tradeoff that costs some
  debuggability.
- **Redaction is regex-only.** Catches structured PII, not names or addresses, and
  number heuristics can false-positive. Right for short previews; not a compliance
  control.
- **Monkey-patching is implicit by nature** and more fragile than wrapping. Mitigated:
  `instrument()` raises if a target is missing rather than capturing nothing, tests
  assert the patch against the real SDK surfaces, and `LLM_INSTRUMENTATION=wrapper`
  reverts without a code change.
- **Context is a fixed-size message window,** not summarisation or retrieval —
  predictable, but it silently forgets long conversations.
- **The `messages` table stores unredacted text.** Redaction protects telemetry,
  which fans out to a broker, aggregates and a dashboard; the conversation itself
  is the user's own data in their own view.
- **Both services share one Postgres.** Ownership is enforced by discipline rather
  than permissions — pragmatic at this size, and why the rule is stated explicitly.
- **Timestamps are naive-UTC, not `TIMESTAMPTZ`.** Postgres converts an aware
  datetime to the session timezone before dropping the offset, so the convention
  only holds if every write normalises first — enforced by the `UtcDateTime` column
  type and pinned by tests against a non-UTC server.
- **The UIs are builds with no server-rendered fallback.** The original plain-HTML
  pages were removed rather than kept as a second implementation; the cost is that a
  bare checkout can't serve `/` until something runs `npm run build`.

## What I'd improve with more time

- **Authentication and authorization** — the largest gap. `POST /logs` accepts an
  event from any caller and the dashboard is open to anyone who can reach the port.
  Production needs a service credential on ingestion, a session on the dashboard,
  and per-tenant scoping. Deliberate scope, not an oversight: a half-designed auth
  layer would obscure the logging pipeline this project is about.
- **Versioned migrations.** `create_all` never `ALTER`s an existing table, so adding
  a column needs a fresh database. Alembic is the fix.
- **`TIMESTAMPTZ` instead of naive UTC**, which would make a whole class of timezone
  bug structurally impossible. Deferred because it is a column-type change and
  there are no migrations yet.
- **Capture cancels from a disconnecting HTTP client.** Cancellation is captured
  correctly at the SDK level; the HTTP layer above doesn't close the generator
  promptly, so that log is late or missing. It affects both instrumentation modes,
  so it isn't a property of auto-instrumentation. The fix is explicit disconnect
  detection. Relatedly, a stream aborted before the first token leaves a user
  message with no reply — the UI should drop it or offer a retry.
- **Export the sink counters as real metrics.** `dropped` / `failed` should be
  Prometheus counters with alerts. A telemetry pipeline that can quietly lose its
  own events needs to be observable itself.
- **Adopt the OpenTelemetry GenAI conventions properly** — `InferenceLog` mirrors
  them informally; real OTel spans would make the data portable to any backend.
- **Retention and partitioning for `inference_logs`**, and a columnar store
  (ClickHouse) for the aggregate read path at high volume — how Langfuse and
  Helicone split OLTP from OLAP.
- **Batch log delivery** — one event per call today; batching is a cheap throughput
  win under load.
- **Sanitize rendered markdown** with DOMPurify, **cost tracking** per call and
  model, and **error grouping** in the log explorer.
- **Deploy to self-hosted Kubernetes.** The images and Compose topology are
  container-ready; manifests and an ingress are the remaining work.
