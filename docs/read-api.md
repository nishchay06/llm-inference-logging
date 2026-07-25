# Read API design — list, resume, and metrics

The write path came first; these are the reads that make the stored data useful,
and the shape of the reads is what validates (or embarrasses) the schema.

- `GET /conversations` — list conversations (chatbot)
- `GET /conversations/{session_id}` — resume: full message history (chatbot)
- `GET /stats`, `/stats/timeseries`, `/stats/by_model`, `/logs` — metrics and the
  log stream (ingestion)

## How the established tools model this

The field (Langfuse, Helicone, OpenLLMetry/Traceloop, Arize Phoenix, Lunary)
converges on a few ideas worth borrowing rather than reinventing:

- **A two-level data model: the conversation grouping vs. the individual call.**
  Langfuse: *Session → Trace → Generation*; Lunary: *Thread → Run*. That maps
  onto `conversations` + `messages` (app data) vs. `inference_logs` (telemetry),
  joined by `session_id`.
- **"List sessions" and "open a session" are universal read views** — a list
  with timestamp and call count, click through to the full detail.
- **Analytics live on a different read path from the operational store.**
  Postgres answers "get me this one conversation"; a columnar store answers "p95
  latency over a million rows grouped by model, last 24h." This project is
  nowhere near needing ClickHouse, but the *pattern* is why metrics live on the
  telemetry side rather than bolted onto the chat app.

## Decisions

**Single-owner endpoints — no cross-service table reads.** Conversation reads
touch `conversations` + `messages`, so they live on the chatbot. Metrics touch
`inference_logs`, so they live on ingestion. The two services share one physical
Postgres, so *technically* either could query anything; the discipline is that
neither does.

**Derive the list preview; don't store a `title` column.** The list shows each
conversation's first user message, derived at query time. Storing a title would
mean maintaining it, and an LLM-generated title is a feature, not a schema
requirement. Deriving keeps the schema stable.

**One aggregate read for the list, not N+1.** The list needs a message count and
a first-user-message preview per row. The trap is looping conversations and
querying messages for each. Instead it is three set-based queries — the ordered
conversations, a grouped count, and a group-wise-first for the preview — stitched
in Python. Three queries regardless of how many conversations exist.

**Metrics aggregate in the database.** Counts, averages, per-model grouping,
latency percentiles (`percentile_disc`) and time bucketing (floored epoch
`GROUP BY`) all execute in Postgres, so a large window transfers a handful of
rows rather than the whole table. SQLite has neither ordered-set aggregates nor
`extract(epoch …)`, so the code branches on dialect and falls back to computing
in Python for tests. `percentile_disc` is chosen over `percentile_cont`
specifically so both branches return the same value — an actually observed
latency, matching the nearest-rank definition — rather than the two backends
disagreeing subtly.

**Pagination is bounded.** `GET /logs` caps `limit` at 500. An unbounded limit on
an unauthenticated read endpoint is a free way to make the service materialise
the entire table.

**Read contracts are separate Pydantic models, not the SQLModel tables.**
`ConversationSummary`, `ConversationDetail`, `MessageOut`, `StatsOut`, `LogItem`
— the same wire-≠-storage discipline applied to reads, so the API response shape
does not silently follow a schema change.

## Contracts

### `GET /conversations`
Ordered by `updated_at` desc.

```json
[{
  "session_id": "…",
  "preview": "first user message, truncated to ~80 chars",
  "message_count": 4,
  "created_at": "…",
  "updated_at": "…"
}]
```

### `GET /conversations/{session_id}`
`404` if unknown. Messages ordered by `id` ascending.

```json
{
  "session_id": "…",
  "created_at": "…",
  "updated_at": "…",
  "messages": [
    {"role": "user", "content": "…", "created_at": "…"},
    {"role": "assistant", "content": "…", "created_at": "…"}
  ]
}
```

### `GET /stats`
All filters optional and shared with `/logs`, so the dashboard's filter bar
scopes the charts and the log stream identically. `avg_latency_ms` and the
percentiles are `null` when the window is empty; `error_rate` is `0.0`.

```json
{
  "since": null,
  "total_calls": 29,
  "success_count": 26,
  "error_count": 3,
  "error_rate": 0.103,
  "avg_latency_ms": 4828.35,
  "p50_ms": 3047.39,
  "p95_ms": 6398.56,
  "p99_ms": 47421.05,
  "total_input_tokens": 4210,
  "total_output_tokens": 1876
}
```
