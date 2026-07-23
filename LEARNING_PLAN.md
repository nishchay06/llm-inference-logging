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

- [ ] GitHub repo with complete source
- [ ] README: setup · architecture overview · schema decisions · tradeoffs ·
      what I'd improve with more time
- [ ] Architecture notes: ingestion flow · logging strategy · scaling
      considerations · failure-handling assumptions
- [ ] Demo: hosted link, screenshots, or Loom
- [ ] Frontend: cancel a conversation · list conversations · resume a
      conversation
- Submit to: **work@ollive.ai**

---

## 7. Where I am

- [x] Plan documented
- [x] **Rung 0 — bare FastAPI app** (routing + Pydantic validation; verified
      valid request works and a bad payload returns 422 without our code running)
- [ ] **Rung 1 — dumbest chatbot** ← next
- [ ] Rung 2 … 8

_Next: `POST /chat` that calls an LLM API once and returns the reply — hardcoded
model, no memory, no logging. The "why" to master: the LLM API is just an HTTP
call; find where token counts live in the response._
