# chatbot-app

An LLM inference logging & ingestion system, built slowly and steadily as a
learning project. See [`LEARNING_PLAN.md`](./LEARNING_PLAN.md) for the roadmap.

**Stack:** FastAPI (Python) · Anthropic (Claude) · Postgres (later) · plain HTML/JS UI (later)

## Current stage

**Rung 3 — the SDK wrapper.** Every Claude call now goes through `TracedClient`
(see [`sdk/DESIGN.md`](./sdk/DESIGN.md)), which captures a structured
`InferenceLog` (model, provider, latency, tokens, status/errors, timestamps,
session id, input/output previews) and hands it to a sink. The chat code in
`app/main.py` contains no logging — capture happens entirely in the wrapper.
The sink currently prints; Rung 4 will POST it to an ingestion service.

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

Watch the uvicorn terminal: for every call it prints the structured
`InferenceLog` the wrapper captured (`---- inference log ----`).

## Known tradeoffs (so far)

- Conversation history lives in process RAM, so it is lost on restart and not
  shared across multiple server processes. Rung 6 moves it into a database.
