# chatbot-app

An LLM inference logging & ingestion system, built slowly and steadily as a
learning project. See [`LEARNING_PLAN.md`](./LEARNING_PLAN.md) for the roadmap.

**Stack:** FastAPI (Python) · Postgres (later) · plain HTML/JS UI (later)

## Current stage

**Rung 0 — bare FastAPI app.** Two endpoints proving routing + Pydantic
request validation. No LLM, no logging, no database yet.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Then open:
- http://127.0.0.1:8000/hello — a GET
- http://127.0.0.1:8000/docs — interactive API docs (try `/echo` here)
