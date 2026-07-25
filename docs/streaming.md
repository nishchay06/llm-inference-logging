# Streaming Design — token-by-token responses (+ cancel)

Stream the model's reply to the UI as it's generated, add a **Cancel** button
(the frontend spec's third behaviour), and keep observability intact — the
inference log is now emitted **when the stream ends**, capturing
**time-to-first-token (TTFT)** alongside total latency.

## The "why"
A single call gives you everything at once; a stream gives you text
incrementally and the *usage/total latency only at the end*. So the wrapper must
**tee** the stream — pass deltas to the caller while accumulating — and emit the
log at close. This is exactly how Langfuse/Helicone instrument streams.

## Inspiration (OSS)
- **Provider SDKs**: Anthropic `client.messages.stream()` → `.text_stream`
  (deltas) + `.get_final_message()` (usage/model); Gemini
  `generate_content_stream()` → chunks with `.text`, usage on the last chunk.
- **Observability tools**: wrap the stream, capture TTFT + total latency +
  accumulated output + usage, emit the observation on stream close.
- **Chat UIs**: **SSE** frames over the wire; **`AbortController`** to cancel.

## Decisions
- **Transport: SSE** (`text/event-stream`) on a new **`POST /chat/stream`**; the
  existing non-streaming `POST /chat` stays (programmatic use + existing tests).
- **TTFT: yes** — add a nullable `ttft_ms` to `InferenceLog` (wire) and
  `InferenceLogRow` (storage). *Schema note:* no Alembic, so `create_all` adds it
  only to a **fresh** DB (Docker volume, or re-running `python -m db.init` on a
  new db). An existing DB needs a one-time
  `ALTER TABLE inference_logs ADD COLUMN ttft_ms double precision;`.
- **Cancel: persist partial + `status="cancelled"`** — store the assistant text
  the user actually saw; emit a `cancelled` log (cancel-rate becomes a metric).
- **Log status** gains `"cancelled"` alongside `"success"`/`"error"` (it's a free
  string column — no schema change beyond `ttft_ms`).

## Design by layer

### 1. Adapter — `stream(client, *, model, messages, max_tokens, **kwargs)`
A generator that **yields text deltas** and `return`s a normalized `ChatResult`
(full text + tokens + model). Each provider hides its own streaming shape here,
symmetric with today's `create`/`parse`:
- **Anthropic**: `with client.messages.stream(...) as s: for t in s.text_stream: yield t`,
  then `m = s.get_final_message()` → `ChatResult(text, m.model, m.usage.input_tokens, m.usage.output_tokens)`.
- **Gemini**: iterate `client.models.generate_content_stream(...)`; yield each
  `chunk.text`; take usage from the last chunk with `usage_metadata`
  (`prompt/candidates_token_count`), model from `chunk.model_version`.

### 2. `TracedClient.stream(...)` — the streaming wrapper
Mirrors `chat()` but as a generator. It pulls deltas from the adapter,
**accumulates** them, yields each onward, records **TTFT on the first delta**,
and emits exactly one `InferenceLog` at the end — handling three endings:
- **completion** (`StopIteration` carries the `ChatResult`) → `status="success"`,
  full `output_preview`, tokens, `ttft_ms`, total latency.
- **cancel / disconnect** (`GeneratorExit` at the `yield`) → `status="cancelled"`,
  **partial** `output_preview` from the accumulated chunks, then re-raise.
- **error** → `status="error"` (+ `error_type/message`), then re-raise.

The log still goes out through the same `QueueSink` (fire-and-forget) — nothing
about the logging path changes, only *when* the event is built.

### 3. Endpoint — `POST /chat/stream`
`StreamingResponse(media_type="text/event-stream")`. Persist the user message,
build the context window, then stream: `data: {"type":"delta","text":…}` per
chunk, a final `data: {"type":"done","session_id":…}`, and on failure
`data: {"type":"error",…}`. After the stream closes, persist the assistant
message (full text; partial on cancel) and bump `conversation.updated_at`. The
log is emitted by the wrapper on close (success/cancelled/error).

### 4. Frontend
Consume the response via `fetch` + `ReadableStream` reader, parse SSE frames, and
append deltas into the assistant bubble live (markdown re-rendered as it grows).
While streaming, **Send** becomes **Cancel**, which calls `AbortController.abort()`
— the fetch aborts, the server sees the disconnect and logs `cancelled`.

## Test plan (TDD, fakes only — no network)
- **Adapter.stream** (Anthropic + Gemini): fake streaming client → yields the
  expected deltas and returns a `ChatResult` with accumulated text + normalized
  tokens + model.
- **`TracedClient.stream` success**: yields deltas and emits one `success` log
  with `ttft_ms` set and `output_preview` = full text.
- **cancel**: consume one delta, `gen.close()` → one `cancelled` log with the
  **partial** `output_preview`.
- **error**: streaming client raises → one `error` log, exception re-raised.
- **schema**: `InferenceLogRow(ttft_ms=…)` round-trips (and stays optional).

Endpoint SSE + the frontend/cancel UX are verified live (they need a real
provider / a browser), not in unit tests.

## Provider note — Gemini thinking models
`gemini-flash-latest` (Gemini 3.x flash) is a **thinking** model: at the default
thinking level it reasons for the whole latency and then emits the answer in one
burst, so the stream shows nothing until the end (TTFT ≈ total latency). The
adapter sets `thinking_config=ThinkingConfig(thinking_level="low")`, which
minimises thinking so tokens actually stream (verified: 9 delta frames over
~700ms for a ~400-word reply). Notes: the old `thinking_budget` knob is
deprecated on 3.x (`thinking_budget=0` → `400`); short replies still arrive in
one burst simply because they finish before spreading (Claude does this too).

## Deferred / tradeoffs
- **Concurrency:** the provider stream is sync; the endpoint runs it via
  Starlette's streaming (threadpool). True async fan-out (async SDK clients)
  is a later refinement.
- **Per-provider system prompts / tool streaming:** out of scope.
- **Token usage on cancel:** unknown mid-stream, so `input/output_tokens` are
  null on a `cancelled` log (TTFT + partial text are still captured).
