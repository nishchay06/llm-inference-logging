# chatbot-app

An LLM inference logging & ingestion system, built slowly and steadily as a
learning project. See [`LEARNING_PLAN.md`](./LEARNING_PLAN.md) for the roadmap.

**Stack:** FastAPI (Python) · Anthropic (Claude) · Postgres (later) · plain HTML/JS UI (later)

## Current stage

**Rung 1 — the dumbest possible chatbot.** A `POST /chat` endpoint that makes
one Claude API call and returns the reply. No memory, no logging, no database
yet. The LLM API is stateless — each call carries only what we send it.

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

Then try the chat endpoint from the interactive docs at
http://127.0.0.1:8000/docs — open `POST /chat`, "Try it out", and send:

```json
{ "message": "Hello! Explain what an API is in one sentence." }
```

Watch the terminal running uvicorn — it prints the inference metadata
(model, tokens, stop reason) that Rung 3 will eventually log.
