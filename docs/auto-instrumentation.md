# Auto-instrumentation — how the app captures telemetry

**This is the default.** Every inference call the app makes is captured by
patching the provider SDK, not by wrapping it at the call site. Nothing in
`app/main.py` times a call, reads token usage, or touches a sink.

The explicit wrapper (`TracedClient`, [sdk-design.md](./sdk-design.md)) was built
first on purpose — build the mechanism you can see before the one that hides
itself — and it remains as the documented alternative and the rollback.

## What it does

`instrument(client, provider, sink)` patches a client's generation methods so a
**plain, un-wrapped** call is captured:

```python
client = instrument(Anthropic(), "anthropic", sink)

with session_scope(session_id):          # optional ambient tag
    client.messages.create(...)          # auto-logged
    with client.messages.stream(...) as s:   # also auto-logged
        for delta in s.text_stream: ...
```

The patched methods **only observe**: they return the provider's raw response, or
the provider's own stream object, unchanged. Errors are logged and **re-raised** —
capture never suppresses.

## Both call shapes are patched

Patching only the non-streaming method would mean a streaming app silently
produces no telemetry at all — no TTFT, no `cancelled` status, nothing. So
`_TARGETS` covers both, and the two providers stream differently:

| Provider | Non-streaming | Streaming | Streaming shape |
|---|---|---|---|
| Anthropic | `messages.create` | `messages.stream` | context manager: text via `.text_stream`, usage via `.get_final_message()` |
| Gemini | `models.generate_content` | `models.generate_content_stream` | plain iterator of chunks; usage on the final chunk(s) |

**Anthropic's context manager** is the delicate case. `messages.stream(...)`
returns a `MessageStreamManager`, so the patch returns a proxy whose `__enter__`
hands back a proxied stream that tees `text_stream`, and whose `__exit__` emits
the log. `__exit__` is the right place because it is the single point that sees
every way a stream can end: normally, via `GeneratorExit` when the consumer walks
away (a cancel), or via a provider exception.

The stream object is **proxied, not mutated**. `text_stream` happens to be an
instance attribute on today's SDK, so reassigning it would work — but proxying
also works if it becomes a property, and never writes to a third-party object.

**Gemini's iterator** is simpler: wrap it in a teeing generator that yields the
provider's chunks untouched, pulling delta text and usage out via small adapter
helpers (`delta_text`, `stream_usage`) so provider knowledge stays in
`sdk/providers.py` rather than leaking into the patch layer.

## One capture core, two mechanisms

Capture lives in `sdk/capture.py` and is shared:

```
              sdk/capture.py   ← timing, TTFT, status, redaction, log building
                 ↙                        ↘
      TracedClient (wraps)          instrument() (patches)
```

That is what makes the two mechanisms genuinely equivalent rather than merely
similar — and the app's tests assert it directly: for the same call, both modes
must record the same `InferenceLog` fields (`tests/test_app_instrumentation.py`).
Verified live too: a non-streaming call through each path produced identical
previews, model, and token counts.

## The client that is left over

With capture ambient, the app's client only has to normalize provider responses.
That is `ProviderClient` (`sdk/client.py`): adapter-backed, returns a
`ChatResult`, and contains **no capture logic at all**. Two tests pin exactly
that — given an un-patched client it emits nothing; given a patched one, exactly
one log per call.

It exposes the same `chat()` / `stream()` surface as `TracedClient`, so the two
are drop-in interchangeable and the endpoints read the same either way.

## Session id without touching the call site

`session_scope(session_id)` publishes the id through a `contextvars` variable —
the OpenTelemetry pattern — and the patch reads it when the call is made.

Two details that were learned the hard way:

- The id is read **at call time**, not at emit time. A stream's log is written
  when it closes, by which point the scope may already have exited.
- The scope restores the previous **value** rather than resetting a `Token`.
  A Token may only be reset in the context that created it, and this scope spans a
  generator whose steps run in different contexts — FastAPI serves a sync
  streaming response through a threadpool. Token-based reset raised
  `Token ... was created in a different Context` at stream close, which surfaced
  as a spurious `error` event at the end of an otherwise perfect stream. It was
  caught in live testing, not by the fakes; `test_stream_can_be_consumed_across_threads`
  now covers it.

## Honest limits

- The Anthropic tee covers the `.text_stream` + `.get_final_message()` access
  pattern — what the adapters and essentially all SDK users use. Iterating raw
  events (`for event in stream:`) is passed through untouched but not captured.
- **Async clients are not patched.** The app is sync throughout.
- Patching is **per client instance**, not per SDK class. Every client the app
  builds goes through `instrument()`, so coverage is complete in practice, and
  per-instance avoids global side effects that would surprise anyone importing
  the SDK. Class-level patching, closer to how OTel instrumentors work, is the
  alternative if truly global capture is ever wanted.
- A missing target method raises `AttributeError` at instrument time,
  deliberately: an SDK that moved a method should fail loudly rather than leave
  the app running with instrumentation that captures nothing. Tests assert the
  patch against the **real** Anthropic and Gemini surfaces so an SDK upgrade
  fails CI.
- A stream abandoned by a **disconnecting HTTP client** is not always logged.
  That is not this layer — cancel is captured correctly at the SDK level, and the
  same behaviour occurs with the wrapper. See the README's improvements.

## Rollback

`LLM_INSTRUMENTATION=wrapper` switches the app back to `TracedClient` with no code
change. It is exercised by the test suite in both modes, and passed through in
`docker-compose.yml`.
