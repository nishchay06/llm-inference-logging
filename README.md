# chatbot-app

An LLM inference logging & ingestion system, built slowly and steadily as a
learning project. See [`LEARNING_PLAN.md`](./LEARNING_PLAN.md) for the roadmap.

**Stack:** FastAPI (Python) · Anthropic (Claude) · Postgres + SQLModel · plain HTML/JS UI (later)

## Current stage

**Rung 6 — persist to Postgres.** Two services: the **chatbot** (`app/`) and a
separate **ingestion** API (`ingestion/`). Every Claude call goes through
`TracedClient` (see [`sdk/DESIGN.md`](./sdk/DESIGN.md)), which captures a
structured `InferenceLog` and hands it to a **`QueueSink`** wrapping an
`HttpSink`: the event is enqueued instantly and a background thread ships it to
ingestion, so log delivery can be slow or fail without ever blocking or breaking
the chat. Data is now persisted in Postgres (see
[`db/DESIGN.md`](./db/DESIGN.md)): the chatbot writes `conversations` +
`messages` (the in-memory store is retired), and ingestion writes
`inference_logs`.

## Setup

Prerequisites: Python 3.11+ and a running **Postgres** server (any local
Postgres works; Docker Compose comes later).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env: paste your API key (+ DATABASE_URL)

createdb chatbot            # create the database (if it doesn't exist)
python -m db.init           # create the tables (one-time)
```

Get an API key at https://console.anthropic.com/ (Settings → API Keys).
`DATABASE_URL` defaults to `postgresql+psycopg://<your-os-user>@localhost:5432/chatbot`.

## Run

Two services, in two terminals (both from the project root, venv activated):

```bash
# terminal 1 — ingestion service
uvicorn ingestion.main:app --port 8001 --reload

# terminal 2 — chatbot
uvicorn app.main:app --port 8000 --reload
```

The chatbot persists conversations and messages to Postgres and ships each
inference log to the ingestion service (`INGESTION_URL`, default
`http://127.0.0.1:8001/logs`), which stores it in `inference_logs`. Inspect it:

```bash
psql chatbot -c "select role, content from messages order by id;"
psql chatbot -c "select model, status, latency_ms, input_tokens, output_tokens from inference_logs;"
```

## Try the memory

Open http://127.0.0.1:8000/docs → `POST /chat`.

1. Send `{ "message": "Hi, my name is Nishchay." }` — the response includes a
   `session_id`.
2. Send `{ "message": "What is my name?", "session_id": "<paste it>" }` — it
   remembers.
3. Send `{ "message": "What is my name?" }` with **no** `session_id` — a fresh
   conversation that does not remember.

Watch the uvicorn terminal: for every call it prints the structured
`InferenceLog` the wrapper captured (`---- inference log ----`).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

Unit tests make no real API calls and open no network connections. The
`TracedClient` tests pass a **fake** Anthropic client and a **fake** sink and
assert on what was captured; the ingestion tests use FastAPI's `TestClient` to
check the payload contract (valid → 200, malformed → 422). The code is testable
because the client and sink are injected — good design and testability go
together.

## Schema design decisions

Three tables, each owned by the service that produces the data:

- `conversations` / `messages` — owned by the **chatbot** (transactional app data).
- `inference_logs` — owned by the **ingestion** service (append-only telemetry).

Messages and inference logs are kept in **separate tables on purpose**: they are
different kinds of data with different volume, lifecycle, and scaling needs. A
conversation is app state a user cares about; an inference log is observability —
one row per LLM call, including retries and errors that never became a visible
message. `inference_logs.session_id` is a plain indexed column, **not** a foreign
key, because telemetry is written independently and may reference a session with
no conversation row.

The **wire model** (`sdk/events.py::InferenceLog`, pure Pydantic) is separate from
the **storage model** (`db/models.py::InferenceLogRow`, SQLModel), so the
transport contract and the database schema can evolve independently.

Schema creation is a one-time `python -m db.init` step, not done on app startup —
two services racing to `CREATE TABLE` on boot causes a concurrent-DDL error, and
schema is a single-owner concern.

## Known tradeoffs (so far)

- No versioned migrations yet: schema is created with SQLModel `create_all` via
  `python -m db.init`. A real system would use **Alembic** — *what I'd improve*.
- Log shipping is non-blocking and failure-safe (in-memory queue + background
  worker), but the queue lives *in the process*: a crash loses queued events,
  and there is no retry or persistence. Rung 8 (an external queue) addresses
  durability.
