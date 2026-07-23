# Rung 7 Design — Close the Loop (reads: list / resume / stats)

The **read** rung. Rungs 0–6 were all writes; now the DB earns its keep. The
"why" to master: **what queries does the product actually need?** The shape of
the reads is what validates (or embarrasses) the schema we chose in Rung 6.

Three endpoints, matching the assignment's named UI behaviours ("The UI allows:
… List conversations, Resume a conversation") plus the seed of the
"Latency + Throughput + Errors dashboards" bonus:

1. `GET /conversations` — list conversations (chatbot service)
2. `GET /conversations/{session_id}` — resume: full message history (chatbot)
3. `GET /stats` — latency / throughput / errors over the logs (ingestion service)

## Inspiration — how the open-source LLM-observability tools do it

We surveyed the field (Langfuse, Helicone, OpenLLMetry/Traceloop, Arize
Phoenix, Lunary) to avoid inventing patterns. The convergent ideas:

- **Two-level data model: the conversation grouping vs. the individual call.**
  Langfuse: *Session → Trace → Generation*; Lunary: *Thread → Run*. This is
  exactly our `conversations` + `messages` (app data) vs. `inference_logs`
  (telemetry), joined by `session_id`. We are aligned with the whole field.
- **"List sessions" and "view/resume a session" are universal read views.**
  Langfuse's Sessions page is a list of sessions (timestamp + trace count) with
  click-through to the full trace — verbatim our list + resume.
- **Analytics live in a separate read path from the operational store.**
  Postgres answers "get this one conversation"; a columnar store (ClickHouse)
  answers "p95 latency over 1M rows grouped by model, last 24h." We don't need
  ClickHouse at our scale, but the *pattern* is why stats belongs on the
  telemetry side (ingestion), not bolted onto the chat app.
- **The dashboard metrics are standardised:** volume/throughput over time,
  latency (avg + p50/p95/p99), error rate, tokens, cost — shown as a time
  series *and* grouped by model/provider. This is the assignment's
  "Latency + Throughput + Errors" exactly.
- **Three auto-instrument strategies exist:** wrapper (ours), proxy (Helicone),
  OTel instrumentor (OpenLLMetry). A README "what I'd improve" note.
- **OpenTelemetry GenAI semantic conventions** (`gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.system`, …) are the emerging standard for
  *what to name* the fields we capture. Our `InferenceLog` mirrors them
  informally — worth citing in the README.

## Decisions

- **Single-owner endpoints (no cross-service table reads).** Conversation reads
  touch `conversations` + `messages` → they live on the **chatbot** (`app/`).
  Stats touch `inference_logs` → they live on **ingestion**. The two services
  share one physical Postgres, so *technically* either could query any table;
  the discipline is that neither reads the other's tables. This preserves the
  Rung 6 ownership split — and mirrors how real tools keep the metrics read path
  separate. (README tradeoff.)

- **New endpoints use `Depends(get_session)`.** The chat endpoint keeps its
  existing direct-`Session(engine)` style (not refactoring beyond the rung), but
  every new read endpoint takes the session via dependency injection — like
  `ingestion.main.ingest` — so tests override it with in-memory SQLite.

- **Derive the list preview; do NOT add a stored `title` column.** Most tools
  derive display info rather than store a title (auto-generated titles are a
  later nice-to-have). Deriving keeps the schema stable and turns the list query
  into an instructive "first user message per conversation" (group-wise-first)
  problem. *Future:* an LLM-generated title column.

- **One aggregate read for the list, not N+1.** The list needs, per row, a
  message count and the first user message. The trap is looping conversations
  and querying messages for each (N+1). We use grouped/joined queries instead —
  this is the core learning of the rung.

- **Stats: shape around the three named families, overall first, time-aware.**
  Latency (avg), throughput (count within an optional window), errors
  (count + rate). Accept optional `?since=<ISO8601>` filtering `started_at >=
  since` (indexed) so "throughput" is calls-in-a-window, not just a row count.
  Designed to grow a `group by model` and time-bucketed series later (the
  dashboard UI); the first cut returns overall figures.

## Endpoint contracts

### `GET /conversations` (chatbot)
Ordered by `updated_at` desc. Response: list of

```json
{
  "session_id": "…",
  "preview": "first user message, truncated to ~80 chars",
  "message_count": 4,
  "created_at": "2026-…",
  "updated_at": "2026-…"
}
```

### `GET /conversations/{session_id}` (chatbot)
`404` if the conversation does not exist. Messages ordered by `id` asc.

```json
{
  "session_id": "…",
  "created_at": "2026-…",
  "updated_at": "2026-…",
  "messages": [
    {"role": "user", "content": "…", "created_at": "2026-…"},
    {"role": "assistant", "content": "…", "created_at": "2026-…"}
  ]
}
```

### `GET /stats?since=<ISO8601>` (ingestion)
`since` optional; when omitted, all-time. Response:

```json
{
  "since": null,
  "total_calls": 3,
  "success_count": 3,
  "error_count": 0,
  "error_rate": 0.0,
  "avg_latency_ms": 2419.27,
  "total_input_tokens": 106,
  "total_output_tokens": 39
}
```

`error_rate` = `error_count / total_calls` (0.0 when no calls). `avg_latency_ms`
is `null` when there are no calls in the window.

## Response models (wire shapes)

New Pydantic response models (not SQLModel tables) — the read contract is
separate from storage, same wire-≠-storage discipline as `InferenceLog`:

- `app/`: `ConversationSummary`, `ConversationDetail`, `MessageOut`
- `ingestion/`: `StatsOut`

## Test-first plan (red → green)

Following the existing pattern (`tests/test_ingestion.py`): in-memory SQLite via
`app.dependency_overrides[get_session]`, `TestClient`, seed rows, assert the
contract. New `tests/conftest.py` sets a dummy `ANTHROPIC_API_KEY` so importing
`app.main` (which constructs `Anthropic()` at module load) doesn't require a real
key; the read tests never call `/chat`.

- **`tests/test_reads.py`** (chatbot):
  - list: empty DB → `[]`; seeded conversations → ordered by `updated_at` desc,
    correct `message_count`, `preview` = first user message.
  - resume: existing session → messages in order; unknown session → `404`.
- **stats** (extend `tests/test_ingestion.py` or new `tests/test_stats.py`):
  - empty DB → zeros, `avg_latency_ms` null, `error_rate` 0.0.
  - mixed success/error rows → correct counts, avg latency, error rate.
  - `?since=` window excludes older rows.

## Deferred (kept out of this rung, on purpose)

- **Pagination** on the list — return all rows now; note as a scaling TODO.
- **"Cancel a conversation"** — it's aborting an in-flight response → pairs with
  the streaming bonus, not here.
- **Time-bucketed series** and **group-by-model** in stats → the dashboard UI.
- **Cross-service combined views** (e.g. conversations with token totals) →
  would force one service to read the other's tables.
- **Frontend** — get the three endpoints curl-verified first, then wire the
  plain HTML/JS (walking skeleton per layer).
