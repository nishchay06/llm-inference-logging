# SDK design — the instrumentation layer

Capture inference metadata around every LLM call **without cluttering the chat
code**. The chat endpoint in `app/main.py` contains no timing, no token
extraction, no logging — it just chats. That is the whole test of this design.

## The one principle: decoupled concerns

| Concern | *What* it is | Lives in |
|---|---|---|
| **Event schema** | what we capture | `sdk/events.py` — `InferenceLog` (Pydantic) |
| **Capture** | timing, TTFT, status, redaction | `sdk/capture.py` |
| **Application** | how capture reaches the call | `sdk/instrument.py` (patch, default) · `sdk/tracing.py` (wrap) |
| **Normalization** | one result shape across providers | `sdk/providers.py` adapters · `sdk/client.py` |
| **Sink** | where it goes | `sdk/sinks.py` — a `Callable[[InferenceLog], None]` |

Capture and *application of* capture are separate rows on purpose. That split is
what let auto-instrumentation become the default without reimplementing streaming
semantics: `instrument()` and `TracedClient` drive the same core, so they record
identical fields — asserted by parity tests rather than assumed.

Keeping them apart is what made every subsequent capability a *swap* rather
than a rewrite:

- Shipping to a service = swap the **sink** (log line → HTTP POST).
- Making delivery safe = wrap the **sink** (`QueueSink`).
- Durable transport = swap the inner **sink** again (`RedisStreamSink`).
- Multi-provider = add an **adapter**; the event schema is unchanged.
- Zero-touch capture = apply the same capture logic by monkey-patch instead of
  by wrapping ([auto-instrumentation.md](./auto-instrumentation.md)).

None of those touched `InferenceLog`, and none touched the chat code.

## The event schema — `InferenceLog`

| Field | Notes |
|---|---|
| `event_id` | uuid for this log record; also the storage primary key, which bounds duplicates on broker replay |
| `session_id` | conversation id (nullable — telemetry can outlive a conversation) |
| `provider` / `model` | e.g. `"anthropic"` / `"claude-sonnet-5"` |
| `status` | `"success"` \| `"error"` \| `"cancelled"` |
| `error_type` / `error_message` | null on success |
| `started_at` / `ended_at` | UTC timestamps |
| `latency_ms` | wall-clock duration |
| `ttft_ms` | time to first token — streaming only |
| `input_tokens` / `output_tokens` | null on error |
| `input_preview` / `output_preview` | truncated to ~200 chars, PII-redacted |

It stays a **pure Pydantic model** with no SQLModel or database import, so the
SDK never depends on the storage layer. The corresponding table lives in
`db/models.py::InferenceLogRow` — see [schema-design.md](./schema-design.md) for
why the wire model and the storage model are deliberately separate types.

The field names mirror the **OpenTelemetry GenAI semantic conventions**
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.system`) informally
rather than adopting them literally; adopting them properly is a follow-up.

## `TracedClient`

Holds the underlying provider client, a `provider` name, and an injected `sink`
callable. Around each call: record start → try → on success compute latency, pull
model/tokens from the parsed response, emit `status="success"`; on failure emit
`status="error"` and **re-raise**.

That re-raise is the important part. The wrapper **observes and never
suppresses** — the caller and FastAPI still handle the failure exactly as if the
instrumentation were not there. Instrumentation that changes program behaviour
under failure is worse than no instrumentation.

`chat()` returns a normalized `ChatResult`, so chat code reads
`traced.chat(...).text` and never sees a provider-specific response shape.

### Streaming

`stream()` is the streaming twin. A stream inverts the problem: text arrives
incrementally but usage and total latency only arrive at the end, so the wrapper
**tees** — yielding each delta to the caller while accumulating — and emits
exactly one log when the stream closes. TTFT is stamped on the first delta.
Closure has three outcomes and each is a distinct status: `success` on normal
completion, `cancelled` on `GeneratorExit` (the consumer stopped iterating, i.e.
the client disconnected), `error` on failure. Details in
[streaming.md](./streaming.md).

## Multi-provider — `sdk/providers.py`

A per-provider **adapter** isolates the two things that differ, behind a uniform
interface: `create()` (how to call it), `parse()` (how to read the response into
normalized fields), and `stream()` (how to iterate deltas). `AnthropicAdapter`
and `GeminiAdapter` register in `ADAPTERS`; `build_client(provider)` constructs
the right SDK client, lazily imported so an unconfigured provider costs nothing.

Gemini is the instructive case: roles are user/**model** not user/assistant,
content is wrapped in `parts`, max tokens goes through a config object, usage
fields are `prompt/candidates_token_count`, and the served model comes back in
`model_version`. It is also a *thinking* model, which broke streaming twice
before the adapter accounted for it — thinking tokens bill against
`max_output_tokens` (so the answer truncated), and unbounded thinking meant no
tokens streamed until the very end (so TTFT was meaningless). Both are handled in
`_gemini_config`. The wrapper never learned any of this.

In production a library like `litellm` would replace these hand-rolled adapters.

## Package layout

```
sdk/
  events.py      InferenceLog — the wire contract, one source of truth
  capture.py     the capture core — timing, TTFT, status, log building
  instrument.py  instrument() — capture applied by monkey-patch (the DEFAULT)
  tracing.py     TracedClient — capture applied by wrapping (the alternative)
  client.py      ProviderClient — normalizes only; capture comes from the patch
  providers.py   per-provider adapters + ChatResult
  sinks.py       HttpSink, RedisStreamSink, QueueSink
  redaction.py   PII scrubbing, applied before any preview is emitted
```

The app builds `ProviderClient` over a patched SDK client by default; see
[auto-instrumentation.md](./auto-instrumentation.md).
