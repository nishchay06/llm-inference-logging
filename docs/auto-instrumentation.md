# Auto-instrumentation — the monkey-patch layer

True zero-touch capture: instrumentation that requires no change at the call
site at all. The explicit `TracedClient` wrapper came first, on purpose — build
the mechanism you understand before the one that hides itself — and this layer
applies exactly the same capture logic by patching instead of wrapping. See
[sdk-design.md](./sdk-design.md) for the wrapper it complements.

## What it does
`instrument(client, provider, sink)` patches the provider's generation method on
a client so a **plain, un-wrapped** call is captured automatically:

```python
client = instrument(Anthropic(), "anthropic", sink)
with session_scope(session_id):
    client.messages.create(model=…, messages=…, max_tokens=…)  # auto-logged
```

Zero change at the call site (beyond an optional ambient `session_scope`). The
patched method **only observes** — it returns the provider's **raw** response
unchanged.

## It reuses everything (only the *mechanism* is new)
The three decoupled concerns pay off again — capture logic is identical to
`TracedClient`, just applied by patching instead of wrapping:
- **Parse** the response via the existing `ADAPTERS[provider].parse` (model,
  tokens, output text).
- Build the same **`InferenceLog`**, hand to the same **sink**.
- Previews go through the same **`_preview`** → **PII redaction applies for
  free**.
- Errors: log `status="error"` + re-raise (observe, don't suppress).

## Design (`sdk/instrument.py`)
- `instrument(client, provider, sink) -> client` — looks up the patch target per
  provider (`messages.create` for Anthropic, `models.generate_content` for
  Gemini), wraps the original, and `setattr`s it back. **Idempotent**: the
  wrapper is tagged `_auto_instrumented`, so re-instrumenting is a no-op (never
  double-patches or double-logs).
- **`session_scope(session_id)`** — a `contextvars`-based ambient context (the
  OpenTelemetry pattern) so the call site stays pristine; the wrapper reads the
  current session, defaulting to `None`.
- **Input preview** is pulled from the call kwargs, provider-aware (Anthropic
  `messages=[{role,content}]`, Gemini `contents=[{role,parts}]`), then run
  through `_preview` (redacted + truncated).
- **Scope:** both providers, the non-streaming `create`/`generate_content` call.
  Streaming auto-instrument is a noted extension (the explicit wrapper already
  streams).

## Relationship to `TracedClient` (both valid entry points)
The app keeps using `TracedClient` on purpose — it returns a normalized
`ChatResult` and supports streaming, which keeps the chat code clean.
Auto-instrument is the **zero-touch** option for arbitrary call sites that just
want capture without changing their code. Neither replaces the other; they share
the adapters, `InferenceLog`, sink, and redaction.

## Test-first / nothing breaks
The **existing suite is the regression guard** — `TracedClient`, the adapters,
and the app are untouched, so all prior tests must stay green. New tests (fakes,
no network):
- plain `client.messages.create(...)` → exactly one success log (right
  provider/model/tokens/output) **and** the raw response returned unchanged
- error → error log + re-raise
- `session_scope` → log carries the session id
- Gemini `generate_content` → normalized log
- idempotency → instrument twice, still one log per call
- previews PII-redacted (reuses `_preview`)
- patches the **real** SDK surface (instrument an `Anthropic()` instance, assert
  `messages.create` was replaced) — no network call
