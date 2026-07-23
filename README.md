# chatbot-app

An LLM inference logging & ingestion system, built slowly and steadily as a
learning project. See [`LEARNING_PLAN.md`](./LEARNING_PLAN.md) for the roadmap.

**Stack:** FastAPI (Python) · Anthropic (Claude) · Postgres (later) · plain HTML/JS UI (later)

## Current stage

**Rung 5 — safe, non-blocking logging.** Two services: the **chatbot** (`app/`)
and a separate **ingestion** API (`ingestion/`). Every Claude call goes through
`TracedClient` (see [`sdk/DESIGN.md`](./sdk/DESIGN.md)), which captures a
structured `InferenceLog` and hands it to a **`QueueSink`** wrapping an
`HttpSink`. The event is enqueued instantly; a background thread ships it to
ingestion. So log delivery can be slow or fail without ever blocking or breaking
the chat — if ingestion is down, `/chat` still returns 200 and the event is
dropped with a warning. Ingestion validates each payload against the *same*
`InferenceLog` schema (the contract between the services). Rung 6 stores it in a
database.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Add your API key:
cp .env.example .env        # then edit .env and paste your real key
```

Get an API key at https://console.anthropic.com/ (Settings → API Keys).

## Run

Two services, in two terminals (both from the project root, venv activated):

```bash
# terminal 1 — ingestion service
uvicorn ingestion.main:app --port 8001 --reload

# terminal 2 — chatbot
uvicorn app.main:app --port 8000 --reload
```

The chatbot ships logs to the ingestion service at `INGESTION_URL`
(default `http://127.0.0.1:8001/logs`). Watch terminal 1 to see each
`InferenceLog` arrive.

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

## Known tradeoffs (so far)

- Conversation history lives in process RAM, so it is lost on restart and not
  shared across multiple server processes. Rung 6 moves it into a database.
- Log shipping is non-blocking and failure-safe (in-memory queue + background
  worker), but the queue lives *in the process*: a crash loses queued events,
  and there is no retry or persistence. Rung 8 (an external queue) addresses
  durability.
