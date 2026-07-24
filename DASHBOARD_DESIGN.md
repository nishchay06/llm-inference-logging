# Dashboard Design — an observability console for the inference logs

Turn `inference_logs` into a **Latency + Throughput + Errors** console — the
bonus deliverable, and the payoff that shows *why the whole system exists*. It
has two planes, like every log console (CloudWatch, Datadog, Grafana, Kibana):

1. **Overview** — aggregate health: KPI cards + latency/throughput charts.
2. **Log Explorer** — the individual log stream: filterable, color-coded,
   click-to-expand — to diagnose failures *and* inspect successful calls.

## Why it lives on the ingestion service
The dashboard is a **read view over `inference_logs`**, which the **ingestion**
service owns (Rung 6 split). So ingestion serves it (`GET /dashboard`) — the
chatbot never reads telemetry it doesn't own. This keeps the single-owner
discipline and mirrors how the chatbot serves its own UI at `/`.

## Inspiration (existing log dashboards)
- **A time histogram + a log stream below it**, bound by a shared filter/search
  bar — the universal troubleshooting layout (Datadog Log Explorer, CloudWatch
  Logs, Grafana Explore, Kibana Discover).
- **Click a log row → expand full detail** (Grafana "Log details", Kibana/Datadog
  row expand, CloudWatch event JSON).
- **Color-code by status/severity**; filter by status, service, time, free text.
- **KPI tiles + latency percentiles (p50/p95/p99)** — avg hides tail latency.
- (Deferred) error **grouping/patterns** — Sentry Issues, Datadog Patterns.

## Layout (one page, ingestion `/dashboard`)
```
Filters:  status ▾  provider ▾  window ▾  search____   ⟳ auto-refresh
── OVERVIEW ──
[Calls] [Error %] [avg + p95 latency] [Tokens]                 KPI cards
Throughput over time  (stacked success/error)                  Chart.js
Latency over time (avg / p95)                                  Chart.js
By-model: model · calls · error% · avg latency                 table
── LOG EXPLORER ──
ts · status(chip) · provider/model · latency(+ttft) · tokens · preview
  └ click → detail: all fields, full error_message, previews, timestamps
[ Load more ]
```
The **same filter bar drives both planes** (e.g. `status=error` scopes the charts
*and* the stream to failures).

## Endpoints (ingestion)
- `GET /stats?since=` — extend with `p50_ms/p95_ms/p99_ms`.
- `GET /stats/timeseries?since=&bucket=` → `[{start, calls, errors, avg_latency_ms}]`
  (success = calls − errors → stacked histogram).
- `GET /stats/by_model?since=` → `[{model, provider, calls, error_rate, avg_latency_ms}]`.
- `GET /logs?status=&provider=&model=&session_id=&q=&since=&limit=&offset=` →
  `{total, items:[…]}`, newest-first. `q` = case-insensitive substring over
  `input_preview`/`output_preview`/`error_message`. Coexists with `POST /logs`
  (write) — GET queries, POST ingests.
- `GET /dashboard` — the page; `ingestion/static/` mount + vendored Chart.js.

## Aggregation location
Percentiles and time-buckets are computed **in Python** (fetch the filtered
rows/columns, aggregate in code), not in SQL: `percentile_cont`/`date_trunc` are
Postgres-only and the tests run on in-memory SQLite. DB-agnostic + unit-testable,
and fine at this scale. **Scaling tradeoff (README):** push aggregation into SQL
and/or a columnar store (ClickHouse) at high telemetry volume.

## Charting
Vendor **Chart.js** locally into `ingestion/static/` (same pattern as
`marked.js`), served via the `/static` mount. Consult the `dataviz` skill for
palette/legibility (light+dark, colorblind-safe status colors) before writing
chart code. Plain HTML/JS otherwise; auto-refresh via `setInterval` polling.

## Data caveat (state in README)
We store input/output **previews (~200 chars)** + `error_type`/`error_message`,
not full payloads. The detail view diagnoses from previews + error text, not full
request/response bodies — a deliberate storage/privacy tradeoff. A real system
would store full payloads (with PII redaction) or link to a trace store.

## Test plan (TDD, SQLite)
- `/logs`: filter by status/provider/model/session; `q` substring; ordering
  (newest-first); pagination (`limit`/`offset` + correct `total`); empty DB.
- `/stats`: percentiles from a known latency set; error_rate.
- `/stats/timeseries`: bucketing (rows in/out of window; per-bucket calls+errors).
- `/stats/by_model`: grouping + per-model error_rate.
- `/dashboard`: serving-shell test; `/static/chart.min.js` served.

## Scope
**First cut = the core two-plane console above.** Deferred: Sentry-style error
grouping / log patterns, timeline brush-to-filter, saved views, cost tracking,
live push (polling suffices), SQL/OLAP aggregation.
