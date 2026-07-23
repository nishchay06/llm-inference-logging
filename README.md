# chatbot-app

An LLM inference logging & ingestion system, built slowly and steadily as a
learning project. See [`LEARNING_PLAN.md`](./LEARNING_PLAN.md) for the roadmap.

**Stack:** FastAPI (Python) · Anthropic (Claude) · Postgres (later) · plain HTML/JS UI (later)

## Current stage

**Rung 2 — multi-turn memory.** `POST /chat` now remembers a conversation. The
model is stateless, so memory is something we build: history is stored per
`session_id` in an in-memory dict and resent (capped to the last N messages —
"short context") on every turn.

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

```bash
uvicorn app.main:app --reload
```

## Try the memory

Open http://127.0.0.1:8000/docs → `POST /chat`.

1. Send `{ "message": "Hi, my name is Nishchay." }` — the response includes a
   `session_id`.
2. Send `{ "message": "What is my name?", "session_id": "<paste it>" }` — it
   remembers.
3. Send `{ "message": "What is my name?" }` with **no** `session_id` — a fresh
   conversation that does not remember.

Watch the uvicorn terminal: it prints `session_id`, token usage, and
`history_len` (which grows by 2 each turn on a reused session).

## Known tradeoffs (so far)

- Conversation history lives in process RAM, so it is lost on restart and not
  shared across multiple server processes. Rung 6 moves it into a database.
