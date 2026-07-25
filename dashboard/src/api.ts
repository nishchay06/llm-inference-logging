// Real backend client — same shape as the design's mock `api`, wired to the
// ingestion service's read endpoints. Relative URLs go through Vite's dev proxy
// to :8001; in production they're same-origin (ingestion serves the app).

export type Filters = { status?: string; provider?: string; q?: string; since?: string | null };

export type Stats = {
  since: string | null;
  total_calls: number;
  success_count: number;
  error_count: number;
  error_rate: number;
  avg_latency_ms: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  total_input_tokens: number;
  total_output_tokens: number;
};
export type TimeseriesPoint = { start: string; calls: number; errors: number; avg_latency_ms: number | null };
export type Timeseries = { bucket_seconds: number; points: TimeseriesPoint[] };
export type ByModelItem = { model: string; provider: string; calls: number; error_rate: number; avg_latency_ms: number | null };
export type LogItem = {
  event_id: string;
  session_id: string | null;
  provider: string;
  model: string;
  status: "success" | "error" | "cancelled";
  error_type: string | null;
  error_message: string | null;
  latency_ms: number;
  ttft_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input_preview: string | null;
  output_preview: string | null;
  started_at: string;
  created_at: string;
};
export type LogsResponse = { total: number; items: LogItem[] };

// Build the query string. "all"/empty filters are omitted — the backend treats
// a present status/provider as an exact match (so "all" must not be sent).
function qs(f: Filters, extra: Record<string, string | number> = {}): string {
  const p = new URLSearchParams();
  if (f.status && f.status !== "all") p.set("status", f.status);
  if (f.provider && f.provider !== "all") p.set("provider", f.provider);
  if (f.q) p.set("q", f.q);
  if (f.since) p.set("since", f.since);
  for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
  return p.toString();
}

export const api = {
  async stats(f: Filters): Promise<Stats> {
    const r = await fetch("/stats?" + qs(f));
    return r.json();
  },
  async timeseries(f: Filters, bucketSeconds: number): Promise<Timeseries> {
    const r = await fetch("/stats/timeseries?" + qs(f, { bucket: bucketSeconds }));
    return r.json();
  },
  async byModel(f: Filters): Promise<ByModelItem[]> {
    const r = await fetch("/stats/by_model?" + qs(f));
    const j = await r.json();
    return j.items; // endpoint wraps the array as {items: [...]}
  },
  async logs(f: Filters, page: { limit: number; offset: number }): Promise<LogsResponse> {
    const r = await fetch("/logs?" + qs(f, { limit: page.limit, offset: page.offset }));
    return r.json();
  },
};
