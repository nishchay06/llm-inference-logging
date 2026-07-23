# LLM Inference Logging System — Learning Plan

> A learning-first roadmap for building the Ollive Fullstack take-home:
> a lightweight **inference logging & ingestion system** for an LLM app.
>
> **Mode:** slowly and steadily — hone fundamentals, understand every "why"
> before climbing to the next rung.
> **Stack:** FastAPI (Python) backend + SDK + ingestion · Postgres · plain
> HTML/JS UI to start (React optional later).
> **Timeline:** pure learning, no hard deadline.

---

## 0. What this project is *really* testing

The assignment title is the tell: *"inference logging and ingestion system."*
The **chatbot is the decoy** — anyone can wire a chat UI to an API. What's
actually being evaluated is whether I can build a **mini LLM-observability
platform** (same category as Langfuse, Helicone, PostHog-for-LLMs).

The three things that matter most:
1. **Auto-instrumentation** — how cleanly the SDK captures metadata without
   cluttering the chat code (they capitalized AUTO-INSTRUMENT on purpose).
2. **Decoupling** — the logging path must never slow down or break the chat.
3. **Schema judgment** — the data model should show I understand the domain.

Nail those three and a merely-fine chatbot is enough.

---

## 1. The mental model — three services

Keep three *roles* in mind even if they start as one app. The separation is
the whole point (it keeps logging decoupled from chat):

```
chatbot/        FastAPI app: chat endpoint + serves the UI
  └─ uses →  sdk/    wrapper around the LLM call (the star of the show)
                └─ ships logs to →
ingestion/      FastAPI app: receives logs, validates, writes to DB
  └─ writes to →  Postgres  ← dashboard reads from here
```

**The single most important design question in the whole project:**
*How does a log get from the SDK to the database, and what happens on that
path when something goes wrong?* Everything else is comparatively mechanical.

---

## 2. Why FastAPI (the "which teacher" decision)

- **FastAPI** — lightweight, modern async Python. Async logging + Pydantic
  validation map directly onto the requirements. Hand-build more → learn more.
  **← chosen.**
- Django — teaches ORM/schema/admin well, but its async logging story is
  clunky, and async logging is the heart of this project.
- Node/Express — one language front-to-back, but I want to hone Python here.

Frontend: start with plain HTML + JS so every layer stays visible; add React
later only if I want to learn it.

---

## 3. The learning ladder

Rule: **build each rung crudely → master its one "why" → then climb.**
Don't jump rungs. If you can't write two sentences answering the rung's "why,"
stay on the rung.

| Rung | Build | The "why" to master before climbing |
|------|-------|--------------------------------------|
| **0** | Bare FastAPI app: one `/hello` GET + one Pydantic-validated `/echo` POST. No LLM. | What does Pydantic actually do when a bad payload arrives? What is `async def` / uvicorn doing? |
| **1** | Dumbest chatbot: `POST /chat` → one LLM call → reply. Hardcoded model, no memory, no logging. | The LLM API is just an HTTP call — what exactly am I sending and getting back? (Find where token counts live in the response.) |
| **2** | Multi-turn memory: resend prior messages each turn; cap to last N. | The model is **stateless** — where does history live, and how do I stop it growing forever? ("short context") |
| **3** | The SDK wrapper: around the LLM call capture model, provider, latency, tokens, status/error, timestamps, session ID, input/output previews. | How do I capture all this **without cluttering my chat code**? (wrapper → decorator → auto-instrument) |
| **4** | Ship the log crudely: wrapper does a synchronous POST to an ingestion endpoint that just prints it. | What's the **contract** (payload shape) between SDK and ingestion? |
| **5** | Make logging safe: fire-and-forget, swallow failures, so ingestion being down never affects the chat. | Why must logging **never block or crash** the chat? (Start with FastAPI `BackgroundTasks`.) |
| **6** | Persist: ingestion writes to Postgres. Design tables: conversations, messages, inference_logs. Learn SQLModel + migrations. | Should chat messages and inference logs share a table? Why or why not? (the tradeoff they care about) |
| **7** | Close the loop: list conversations, resume a conversation (frontend spec). Simple stats endpoint = dashboard seed. | Now the DB earns its keep — what queries does the product actually need? |
| **8+** | Bonuses by curiosity: queue/event architecture (replaces rung 5's background task), streaming, multi-provider, Docker Compose, PII redaction, dashboard, k8s. | Each bonus has its own "why"; only start once the core is rock-solid. |

---

## 4. Build method (learning mode)

1. **Walking skeleton first.** Even going slow, get a crude UI → LLM → one DB
   row working end-to-end before deepening any single layer. Seeing the pieces
   talk teaches more than a perfect isolated part.
2. **One "why" per rung.** Before climbing, write yourself two sentences
   answering that rung's question. Can't? Stay.
3. **Deliverables are first-class.** The README (setup, architecture, schema
   decisions, tradeoffs, "what I'd improve") and architecture notes are 2 of
   the 4 graded deliverables — not an afterthought. Cutting scope + explaining
   *why* in the README is evidence of good judgment, not a gap.

---

## 5. Bonus items, ranked by impact-per-hour

- **Cheap + high signal:** Docker Compose one-command setup · multi-provider
  (falls out of a clean wrapper) · streaming responses.
- **Medium:** latency/throughput/errors dashboard (a read view over the logs
  table — secretly high-leverage, it shows *why the system exists*) · PII
  redaction (scrub step in the SDK before logging) · event-based architecture
  (the queue).
- **Expensive / last / skippable:** self-hosted k8s deploy (Docker Compose
  already proves packaging).

---

## 6. Deliverables checklist (from the assignment)

- [x] GitHub repo with complete source
- [x] README: setup · architecture overview · schema decisions · tradeoffs ·
      what I'd improve with more time
- [x] Architecture notes: ingestion flow · logging strategy · scaling
      considerations · failure-handling assumptions (`ARCHITECTURE.md`)
- [ ] Demo: hosted link, screenshots, or Loom ← **screenshots pending (manual)**
- [~] Frontend: list conversations [x] · resume a conversation [x] · cancel a
      conversation [ ] (deferred to the streaming bonus — aborting an in-flight
      response)
- Submit to: **work@ollive.ai**

---

## 7. Where I am

- [x] Plan documented
- [x] **Rung 0 — bare FastAPI app** (routing + Pydantic validation; verified
      valid request works and a bad payload returns 422 without our code running)
- [x] **Rung 1 — dumbest chatbot** (`POST /chat` → one Claude call → reply.
      Verified live: reply returned, and model/stop_reason/token usage printed
      from the response object. Model: claude-sonnet-5. No memory, no logging.)
- [x] **Rung 2 — multi-turn memory** (per-`session_id` history in an in-memory
      dict, resent capped to the last N messages. Verified: a reused session
      recalls a name; a new session does not; `history_len` grows 2→4 per turn.)
- [x] **Rung 3 — the SDK wrapper** (`sdk/`: `InferenceLog` schema, `TracedClient`
      that times/captures/emits around each call, pluggable `emit` sink. Chat
      code has zero logging. Verified success + error capture; design in
      `sdk/DESIGN.md`.)
- [x] **Rung 4 — ship the log to ingestion** (`ingestion/` service + `HttpSink`.
      Verified: log crosses the network and is validated against the shared
      `InferenceLog` schema; with ingestion DOWN, `/chat` 500s — the coupling
      Rung 5 fixes.)
- [x] **Rung 5 — safe, non-blocking logging** (`QueueSink`: enqueue instantly,
      background worker delivers and swallows failures. Built test-first
      (red→green). Verified end-to-end: with ingestion DOWN, `/chat` returns 200.)
- [x] **Rung 6 — persist to Postgres** (SQLModel; `db/` package. Chatbot owns
      conversations+messages (in-memory dict retired), ingestion owns
      inference_logs. Separate tables (app data vs telemetry); wire-model vs
      storage-model split; schema via one-time `python -m db.init` (NOT on
      startup — avoids a concurrent-DDL race between the two services). Verified
      end-to-end: all three tables populate; DB-backed memory works. Design in
      `db/DESIGN.md`.)
- [x] **Rung 7 — close the loop (list/resume conversations + stats)**
      - [x] **Reads backend** (TDD, all live-verified against Postgres):
            `GET /conversations` (list: preview from first user message +
            message_count, ordered by updated_at; one aggregate query, not N+1),
            `GET /conversations/{session_id}` (resume: ordered history, 404 if
            missing) on the chatbot; `GET /stats?since=` (latency avg /
            throughput count / error rate over inference_logs) on ingestion.
            Single-owner endpoints — neither service reads the other's tables.
            Design + OSS-tool inspiration in `RUNG7_DESIGN.md`.
      - [x] **Frontend** — the project's first UI: a single plain HTML/JS page
            (`app/static/index.html`, served at `/`) — chat window + conversation
            sidebar over `/chat`, `/conversations`, `/conversations/{id}`. Covers
            "expose a simple UI" + the named list/resume behaviours. Verified in
            the browser end-to-end; every reply logged (6 logs = 6 replies, 0
            dropped) through the async QueueSink.
- [ ] Rung 8 (bonuses)

_Next: Rung 8 bonuses, by curiosity. Highest impact-per-hour: Docker Compose
one-command setup, multi-provider (falls out of the clean wrapper), streaming
responses (which also unlocks the deferred "cancel a conversation" UI). The
/stats endpoint is the seed of the "Latency + Throughput + Errors dashboards"
bonus. "Cancel a conversation" (the 3rd UI behaviour) is still deferred — it's
aborting an in-flight response, so it pairs with streaming._
