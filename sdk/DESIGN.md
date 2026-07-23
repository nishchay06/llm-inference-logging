# SDK Design — the `TracedClient` wrapper (Rung 3)

The centerpiece of the project: capture inference metadata around every LLM call
**without cluttering the chat code**. This is the "auto-instrument" requirement,
built the honest way — explicit first, so a later monkey-patch layer is a thin
drop-in rather than magic we don't understand.

## The one principle: three decoupled concerns

| Concern | *What* it is | Lives in |
|---|---|---|
| **Event schema** | what we capture (a structured record) | `sdk/events.py` — `InferenceLog` (Pydantic) |
| **Instrumentation** | how we capture it (time + try/except around the call) | `sdk/tracing.py` — `TracedClient` |
| **Sink** | where it goes | `sdk/sinks.py` — `emit(event)` |

Keeping these separate is what makes every later rung a *swap*, not a rewrite:
- Rung 4 (ingestion) = swap the **sink** (`print` → HTTP POST).
- Rung 5 (safe logging) = make the **sink** non-blocking/failure-safe.
- Multi-provider (bonus) = add a **schema** adapter per provider → same `InferenceLog`.
- Auto-instrument (bonus) = apply the **same** capture logic via monkey-patch.

## Package layout

```
app/main.py          ← chat code goes back to being JUST chat code
sdk/
  __init__.py
  events.py          ← InferenceLog: the event schema (one source of truth,
                        reused by ingestion in Rung 4 and the DB in Rung 6)
  tracing.py         ← TracedClient: wraps the call, times it, catches errors,
                        builds an InferenceLog, hands it to the sink
  sinks.py           ← emit(event): prints for now
```

## The event schema — `InferenceLog`

Maps 1:1 to the assignment's metadata list, plus an id:

| Field | Notes | Assignment item |
|---|---|---|
| `event_id` | uuid for this log record | (our own) |
| `session_id` | conversation id | conversation/session ID |
| `provider` | e.g. `"anthropic"` | provider |
| `model` | e.g. `"claude-sonnet-5"` | model |
| `status` | `"success"` \| `"error"` | request status |
| `error_type`, `error_message` | null on success | errors |
| `started_at`, `ended_at` | UTC timestamps | timestamps |
| `latency_ms` | wall-clock duration | latency |
| `input_tokens`, `output_tokens` | null on error | token usage |
| `input_preview`, `output_preview` | truncated (~200 chars) | input/output previews |

## `TracedClient` responsibilities

- Holds: the underlying provider client, a `provider` name, and a `sink` callable
  (dependency injection — the sink is passed in, not hardcoded).
- Exposes `chat(...)` whose surface **mirrors the raw SDK** and returns the raw
  response — so callers treat it as a drop-in. (That transparency is exactly what
  lets auto-instrument replace it invisibly later.)
- Around the call: record start time → try the call →
  - **success:** compute latency, pull model/tokens from the response, build
    `InferenceLog(status="success")`, emit.
  - **error:** build `InferenceLog(status="error", error_type/message=...)`, emit,
    then **re-raise** — we only *observe* the failure; the caller/FastAPI still
    handles it.

## Deliberately deferred

- **Non-blocking / failure-safe sink** → Rung 5 (this rung's sink is a plain print).
- **Sending over HTTP** → Rung 4.
- **Auto-instrument (monkey-patch)** → bonus, after the core is solid.

## The "why" to master before climbing

*How do we capture all this without cluttering the chat code?* — verify by
looking at how clean `app/main.py`'s `chat()` becomes: no timing, no token
extraction, no metadata print. Just chat, calling `traced.chat(...)`.

## Build steps

1. `sdk/events.py` — the `InferenceLog` model.
2. `sdk/sinks.py` — `emit()` prints the event.
3. `sdk/tracing.py` — `TracedClient`.
4. `app/main.py` — use `traced.chat(...)`, delete the inline metadata print.
5. Verify: `/chat` still works and prints a structured `InferenceLog`; a bad
   model triggers the error branch (`status="error"` captured), then re-raises.
