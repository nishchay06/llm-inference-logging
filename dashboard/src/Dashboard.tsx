import React from "react";
import { api, Filters, Stats, Timeseries, ByModelItem, LogItem } from "./api";
import { buildBarChart, buildLineChart } from "./charts";
import { nfmt, msFmt, pctFmt, timeShort, fullTime, STATUS, MONO } from "./format";

const REFRESH_MS = 5000;
const PAGE = 10;
const muted = (p: number) => `color-mix(in srgb, var(--color-text) ${p}%, transparent)`;

type FilterState = { status: string; provider: string; window: string; q: string };
type State = {
  theme: "light" | "dark";
  filters: FilterState;
  autoRefresh: boolean;
  loading: boolean;
  stats: Stats | null;
  timeseries: Timeseries | null;
  byModel: ByModelItem[];
  logs: LogItem[];
  logTotal: number;
  offset: number;
  expanded: Record<string, boolean>;
};

export default class Dashboard extends React.Component<{}, State> {
  _auto: any = null;
  _qTimer: any = null;

  state: State = {
    theme: "light",
    filters: { status: "all", provider: "all", window: "all", q: "" },
    autoRefresh: false,
    loading: true,
    stats: null,
    timeseries: null,
    byModel: [],
    logs: [],
    logTotal: 0,
    offset: 0,
    expanded: {},
  };

  componentDidMount() {
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    this.setState({ theme: prefersDark ? "dark" : "light" }, () => this.reloadAll(true));
  }
  componentWillUnmount() { this.stopAuto(); clearTimeout(this._qTimer); }

  windowSince(w: string): string | null {
    if (w === "all") return null;
    const map: Record<string, number> = { "15m": 15 * 60000, "1h": 3600000, "24h": 24 * 3600000 };
    return new Date(Date.now() - (map[w] || 0)).toISOString();
  }
  windowBucket(w: string): number { return ({ "15m": 60, "1h": 300, "24h": 3600, all: 1500 } as any)[w] || 1500; }
  apiFilters(): Filters {
    const f = this.state.filters;
    return { status: f.status, provider: f.provider, q: f.q.trim(), since: this.windowSince(f.window) };
  }

  async reloadAll(resetLogs: boolean) {
    const f = this.apiFilters();
    const offset = resetLogs ? 0 : this.state.offset;
    this.setState({ loading: true });
    try {
      const [stats, ts, byModel, logsRes] = await Promise.all([
        api.stats(f),
        api.timeseries(f, this.windowBucket(this.state.filters.window)),
        api.byModel(f),
        api.logs(f, { limit: PAGE, offset }),
      ]);
      this.setState({ stats, timeseries: ts, byModel, logs: logsRes.items, logTotal: logsRes.total, offset, loading: false });
    } catch (e) {
      this.setState({ loading: false });
    }
  }

  async loadMore() {
    const f = this.apiFilters();
    const nextOffset = this.state.offset + PAGE;
    this.setState({ loading: true });
    const res = await api.logs(f, { limit: PAGE, offset: nextOffset });
    this.setState((s) => ({ logs: [...s.logs, ...res.items], offset: nextOffset, logTotal: res.total, loading: false }));
  }

  startAuto() { this.stopAuto(); this._auto = setInterval(() => this.reloadAll(true), REFRESH_MS); }
  stopAuto() { if (this._auto) { clearInterval(this._auto); this._auto = null; } }

  setFilter(patch: Partial<FilterState>, debounce?: boolean) {
    this.setState((s) => ({ filters: { ...s.filters, ...patch }, expanded: {} }), () => {
      if (debounce) { clearTimeout(this._qTimer); this._qTimer = setTimeout(() => this.reloadAll(true), 320); }
      else this.reloadAll(true);
    });
  }

  toggleExpand(id: string) { this.setState((s) => ({ expanded: { ...s.expanded, [id]: !s.expanded[id] } })); }
  toggleAuto = () =>
    this.setState((x) => ({ autoRefresh: !x.autoRefresh }), () => (this.state.autoRefresh ? this.startAuto() : this.stopAuto()));

  detailFor(l: LogItem) {
    const row = (k: string, v: any, opt?: { color?: string; mono?: boolean }) => ({
      k,
      v: v == null || v === "" ? "—" : String(v),
      color: (opt && opt.color) || "var(--color-text)",
      font: opt && opt.mono ? MONO : "var(--font-body)",
    });
    return [
      row("event_id", l.event_id, { mono: true }),
      row("session_id", l.session_id, { mono: true }),
      row("status", STATUS[l.status].label, { color: STATUS[l.status].color }),
      row("provider / model", l.provider + " / " + l.model),
      row("started_at", fullTime(l.started_at)),
      row("created_at", fullTime(l.created_at)),
      row("latency_ms", nfmt(l.latency_ms)),
      row("ttft_ms", l.ttft_ms == null ? null : msFmt(l.ttft_ms)),
      row("input_tokens", l.input_tokens == null ? null : nfmt(l.input_tokens)),
      row("output_tokens", l.output_tokens == null ? null : nfmt(l.output_tokens)),
      row("error_type", l.error_type, { color: l.error_type ? "#dc2626" : "var(--color-text)" }),
      row("error_message", l.error_message, { color: l.error_message ? "#dc2626" : "var(--color-text)", mono: true }),
      row("input_preview", l.input_preview),
      row("output_preview", l.output_preview),
    ];
  }

  render() {
    const s = this.state;
    const st = s.stats;
    const isDark = s.theme === "dark";
    const gridCols = "132px 118px minmax(150px,1.2fr) 104px 118px minmax(180px,2fr)";
    const points = s.timeseries ? s.timeseries.points : [];
    const hasData = points.some((p) => p.calls > 0);
    const kErr = st && st.error_rate > 0;

    const field = (id: string, label: string, control: React.ReactNode) => (
      <div className="field" style={{ margin: 0 }}>
        <label htmlFor={id}>{label}</label>
        {control}
      </div>
    );

    return (
      <div data-theme={s.theme} style={{ minHeight: "100vh", background: "var(--color-bg)", color: "var(--color-text)", fontFamily: "var(--font-body)", display: "flex", flexDirection: "column" }}>
        {/* ── Header ── */}
        <header className="nav" style={{ gap: 14, position: "relative", zIndex: 5 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 14, height: 14, background: "var(--color-accent)" }} />
            <div className="nav-brand" style={{ fontSize: 16 }}>OBSERVE</div>
            <div style={{ fontSize: 11, letterSpacing: "0.14em", textTransform: "uppercase", color: muted(48), borderLeft: "2px solid var(--color-divider)", paddingLeft: 12 }}>Inference Console</div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginLeft: "auto" }}>
            {s.loading && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: muted(55) }}>
                <span style={{ width: 12, height: 12, border: "2px solid var(--color-neutral-400)", borderTopColor: "var(--color-accent)", animation: "om-spin .7s linear infinite", display: "inline-block" }} />
                Updating
              </span>
            )}
            <button className="btn btn-icon btn-secondary" aria-label="Toggle color theme" onClick={() => this.setState((x) => ({ theme: x.theme === "dark" ? "light" : "dark" }))}>
              {isDark ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="square"><circle cx="12" cy="12" r="4" /><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1l2.1-2.1M17 7l2.1-2.1" /></svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="square"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
              )}
            </button>
          </div>
        </header>

        {/* ── Filter bar ── */}
        <div style={{ position: "sticky", top: 0, zIndex: 4, background: "var(--color-bg)", borderBottom: "2px solid var(--color-divider)" }}>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-end", gap: "14px 18px", padding: "14px 24px" }}>
            {field("f-status", "Status",
              <select id="f-status" className="input" value={s.filters.status} onChange={(e) => this.setFilter({ status: e.target.value })} style={{ minWidth: 150, minHeight: 38 }}>
                <option value="all">All</option><option value="success">Success</option><option value="error">Error</option><option value="cancelled">Cancelled</option>
              </select>
            )}
            {field("f-provider", "Provider",
              <select id="f-provider" className="input" value={s.filters.provider} onChange={(e) => this.setFilter({ provider: e.target.value })} style={{ minWidth: 150, minHeight: 38 }}>
                <option value="all">All</option><option value="anthropic">anthropic</option><option value="gemini">gemini</option>
              </select>
            )}
            {field("f-window", "Window",
              <select id="f-window" className="input" value={s.filters.window} onChange={(e) => this.setFilter({ window: e.target.value })} style={{ minWidth: 150, minHeight: 38 }}>
                <option value="all">All time</option><option value="15m">Last 15m</option><option value="1h">Last 1h</option><option value="24h">Last 24h</option>
              </select>
            )}
            <div className="field" style={{ margin: 0, flex: 1, minWidth: 200 }}>
              <label htmlFor="f-search">Search</label>
              <input id="f-search" className="input" type="search" placeholder="Model, error, preview, id…" value={s.filters.q} onChange={(e) => this.setFilter({ q: e.target.value }, true)} style={{ minHeight: 38 }} />
            </div>
            <button
              className="btn btn-secondary" aria-pressed={s.autoRefresh} onClick={this.toggleAuto}
              style={{ minHeight: 38, gap: 9, background: s.autoRefresh ? "var(--color-accent)" : "transparent", color: s.autoRefresh ? "var(--color-bg)" : "var(--color-text)", borderColor: s.autoRefresh ? "var(--color-accent)" : "var(--color-divider)" }}
            >
              <span style={{ width: 8, height: 8, background: s.autoRefresh ? "var(--color-bg)" : muted(45), animation: s.autoRefresh ? "om-pulse 1.6s ease-in-out infinite" : undefined }} />
              Auto-refresh {s.autoRefresh ? "on" : "off"}
            </button>
          </div>
        </div>

        {/* ── Main ── */}
        <main className="om-scroll" style={{ flex: 1, overflowX: "hidden" }}>
          <div style={{ maxWidth: 1240, margin: "0 auto", padding: "22px 24px 40px", display: "flex", flexDirection: "column", gap: 22 }}>

            {/* KPI cards */}
            <section aria-label="Key metrics" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(210px,1fr))", gap: 16 }}>
              <Kpi kicker="Total calls" value={st ? nfmt(st.total_calls) : "—"} meta={s.filters.window === "all" ? "all time" : "in window"} />
              <Kpi kicker="Error rate" value={st ? pctFmt(st.error_rate) : "—"} valueColor={kErr ? "#dc2626" : undefined} meta={errMeta(st)} />
              <Kpi kicker="Latency" value={st ? msFmt(st.avg_latency_ms) : "—"} meta={`p95 ${st ? msFmt(st.p95_ms) : "—"}`} />
              <Kpi kicker="Tokens" value={st ? nfmt(st.total_input_tokens) : "—"} valueSuffix=" in" meta={`${st ? nfmt(st.total_output_tokens) : "0"} out`} />
            </section>

            {/* Charts */}
            <section style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(340px,1fr))", gap: 16 }}>
              <div className="card">
                <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, marginBottom: 10 }}>
                  <div className="card-title" style={{ margin: 0 }}>Throughput</div>
                  <div style={{ display: "flex", gap: 14, fontSize: 11, letterSpacing: "0.06em", textTransform: "uppercase" }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><span style={{ width: 9, height: 9, background: "#0d9488" }} />Success</span>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><span style={{ width: 9, height: 9, background: "#dc2626" }} />Error</span>
                  </div>
                </div>
                {s.timeseries && !hasData
                  ? <ChartEmpty />
                  : <div className="om-scroll" style={{ overflowX: "auto" }}>{buildBarChart(points)}</div>}
              </div>
              <div className="card">
                <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, marginBottom: 10 }}>
                  <div className="card-title" style={{ margin: 0 }}>Avg latency</div>
                  <div style={{ fontSize: 11, letterSpacing: "0.06em", textTransform: "uppercase", display: "inline-flex", alignItems: "center", gap: 6 }}><span style={{ width: 14, height: 2, background: "var(--color-accent)" }} />ms / bucket</div>
                </div>
                {s.timeseries && !hasData
                  ? <ChartEmpty />
                  : <div className="om-scroll" style={{ overflowX: "auto" }}>{buildLineChart(points)}</div>}
              </div>
            </section>

            {/* By model */}
            <section className="card">
              <div className="card-title" style={{ margin: "0 0 12px" }}>By model</div>
              <div className="om-scroll" style={{ overflowX: "auto" }}>
                <table className="table" style={{ minWidth: 560 }}>
                  <thead><tr><th>Model</th><th>Provider</th><th style={{ textAlign: "right" }}>Calls</th><th style={{ textAlign: "right" }}>Error rate</th><th style={{ textAlign: "right" }}>Avg latency</th></tr></thead>
                  <tbody>
                    {s.byModel.map((r, i) => (
                      <tr key={i}>
                        <td style={{ fontWeight: 600 }}>{r.model}</td>
                        <td>{r.provider}</td>
                        <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{nfmt(r.calls)}</td>
                        <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", color: r.error_rate > 0 ? "#dc2626" : muted(55) }}>{pctFmt(r.error_rate)}</td>
                        <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{msFmt(r.avg_latency_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!s.loading && s.byModel.length === 0 && <div style={{ padding: "16px 4px", color: muted(45), fontSize: 13 }}>No matching calls.</div>}
              </div>
            </section>

            {/* Log explorer */}
            <section className="card">
              <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, margin: "0 0 12px" }}>
                <div className="card-title" style={{ margin: 0 }}>Log explorer</div>
                <div style={{ fontSize: 12, color: muted(55) }}>{nfmt(s.logs.length)} of {nfmt(s.logTotal)}</div>
              </div>
              <div className="om-scroll" style={{ overflowX: "auto" }}>
                <div style={{ minWidth: 840 }}>
                  <div style={{ display: "grid", gridTemplateColumns: gridCols, gap: "0 14px", padding: "0 10px 8px", borderBottom: "2px solid var(--color-divider)", fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: muted(50) }}>
                    <div>Time</div><div>Status</div><div>Provider / model</div><div style={{ textAlign: "right" }}>Latency</div><div style={{ textAlign: "right" }}>Tokens</div><div>Preview</div>
                  </div>
                  {s.logs.map((l) => {
                    const meta = STATUS[l.status];
                    const expanded = !!s.expanded[l.event_id];
                    const preview = l.status === "error" ? (l.error_message || "—") : (l.input_preview || "—");
                    return (
                      <div key={l.event_id}>
                        <div
                          className="om-logrow" role="button" tabIndex={0} aria-expanded={expanded}
                          onClick={() => this.toggleExpand(l.event_id)}
                          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.toggleExpand(l.event_id); } }}
                          style={{ display: "grid", gridTemplateColumns: gridCols, gap: "0 14px", padding: 10, borderBottom: "1px solid var(--color-divider)", cursor: "pointer", alignItems: "center" }}
                        >
                          <div style={{ fontVariantNumeric: "tabular-nums", fontSize: 12.5 }}>{timeShort(l.created_at)}</div>
                          <div style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 12, fontWeight: 600 }}>
                            <span style={{ width: 9, height: 9, flex: "none", background: meta.color }} />
                            <span style={{ color: meta.color }}>{meta.label}</span>
                          </div>
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{l.model}</div>
                            <div style={{ fontSize: 11, color: muted(50) }}>{l.provider}</div>
                          </div>
                          <div style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                            <div style={{ fontSize: 13 }}>{msFmt(l.latency_ms)}</div>
                            <div style={{ fontSize: 11, color: muted(50) }}>ttft {msFmt(l.ttft_ms)}</div>
                          </div>
                          <div style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", fontSize: 12.5 }}>
                            {(l.input_tokens == null ? "—" : nfmt(l.input_tokens))} / {(l.output_tokens == null ? "—" : nfmt(l.output_tokens))}
                          </div>
                          <div style={{ minWidth: 0, fontSize: 12.5, color: muted(72), whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{preview}</div>
                        </div>
                        {expanded && (
                          <div style={{ background: "var(--color-neutral-100)", borderBottom: "1px solid var(--color-divider)", padding: "16px 10px 18px" }}>
                            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(230px,1fr))", gap: "12px 22px" }}>
                              {this.detailFor(l).map((d, di) => (
                                <div key={di} style={{ minWidth: 0 }}>
                                  <div style={{ fontSize: 10.5, letterSpacing: "0.09em", textTransform: "uppercase", color: muted(48), marginBottom: 2 }}>{d.k}</div>
                                  <div style={{ fontSize: 12.5, wordBreak: "break-word", color: d.color, fontFamily: d.font }}>{d.v}</div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {!s.loading && s.logs.length === 0 && <div style={{ padding: "22px 10px", color: muted(45), fontSize: 13 }}>No log entries match the current filters.</div>}
                </div>
              </div>
              {s.logs.length < s.logTotal && (
                <div style={{ paddingTop: 14 }}>
                  <button className="btn btn-secondary" onClick={() => this.loadMore()}>Load more</button>
                </div>
              )}
            </section>
          </div>
        </main>
      </div>
    );
  }
}

function Kpi({ kicker, value, valueColor, valueSuffix, meta }: { kicker: string; value: string; valueColor?: string; valueSuffix?: string; meta: string }) {
  return (
    <div className="card">
      <div className="card-kicker">{kicker}</div>
      <div style={{ fontFamily: "var(--font-heading)", fontWeight: 800, fontSize: 34, lineHeight: 1, margin: "8px 0 6px", letterSpacing: "-0.02em", color: valueColor }}>
        {value}
        {valueSuffix && <span style={{ fontSize: 15, fontWeight: 600, color: muted(50) }}>{valueSuffix}</span>}
      </div>
      <div className="card-meta">{meta}</div>
    </div>
  );
}

/** Errors and cancellations are different outcomes: a user pressing Cancel is not
 *  a service failure, so it is reported next to the error count, never inside it. */
function errMeta(st: Stats | null) {
  if (!st) return "0 errors";
  const errors = `${nfmt(st.error_count)} ${st.error_count === 1 ? "error" : "errors"}`;
  return st.cancelled_count > 0 ? `${errors} · ${nfmt(st.cancelled_count)} cancelled` : errors;
}

function ChartEmpty() {
  return <div style={{ height: 240, display: "grid", placeItems: "center", color: muted(45), fontSize: 13 }}>No data in range</div>;
}
