import React from "react";
import { api, Message, Provider, ConversationSummary } from "./api";
import { parseMarkdown } from "./markdown";

const SHOW_MODEL_LABELS = true;

type State = {
  loading: boolean;
  providers: Provider[];
  selectedProvider: string;
  conversations: ConversationSummary[];
  activeSessionId: string | null;
  messages: Message[];
  input: string;
  isStreaming: boolean;
  isTyping: boolean;
  streamText: string;
  theme: "light" | "dark";
  isMobile: boolean;
  sidebarOpen: boolean;
  loadingConv: boolean;
};

const muted = (pct: number) => `color-mix(in srgb, var(--color-text) ${pct}%, transparent)`;

export default class Chat extends React.Component<{}, State> {
  cache: Record<string, Message[]> = {};
  _scroll: HTMLDivElement | null = null;
  _abort: AbortController | null = null;
  _mql: MediaQueryList | null = null;
  _onMq: (e: MediaQueryListEvent) => void = () => {};

  state: State = {
    loading: true,
    providers: [],
    selectedProvider: "",
    conversations: [],
    activeSessionId: null,
    messages: [],
    input: "",
    isStreaming: false,
    isTyping: false,
    streamText: "",
    theme: "light",
    isMobile: false,
    sidebarOpen: false,
    loadingConv: false,
  };

  componentDidMount() {
    const mql = window.matchMedia("(max-width: 780px)");
    this._mql = mql;
    this._onMq = (e) => this.setState({ isMobile: e.matches });
    mql.addEventListener("change", this._onMq);
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    this.setState({ isMobile: mql.matches, theme: prefersDark ? "dark" : "light" });
    Promise.all([api.listProviders(), api.listConversations()])
      .then(([pr, convs]) =>
        this.setState({ providers: pr.providers, selectedProvider: pr.default, conversations: convs, loading: false })
      )
      .catch(() => this.setState({ loading: false }));
  }

  componentWillUnmount() {
    if (this._mql) this._mql.removeEventListener("change", this._onMq);
    if (this._abort) this._abort.abort();
  }

  setScrollRef = (el: HTMLDivElement | null) => { this._scroll = el; };
  scrollToBottom = () => { if (this._scroll) this._scroll.scrollTop = this._scroll.scrollHeight; };

  modelFor(name?: string) {
    const p = this.state.providers.find((x) => x.name === name);
    return p ? p.model : "Assistant";
  }

  selectConversation = async (id: string) => {
    if (id === this.state.activeSessionId) { this.setState({ sidebarOpen: false }); return; }
    this.setState({ activeSessionId: id, sidebarOpen: false, loadingConv: true, streamText: "", isTyping: false });
    let msgs = this.cache[id];
    if (!msgs) { const d = await api.getConversation(id); msgs = d.messages; this.cache[id] = msgs; }
    this.setState({ messages: msgs, loadingConv: false }, this.scrollToBottom);
  };

  newChat = () =>
    this.setState({ activeSessionId: null, messages: [], input: "", sidebarOpen: false, streamText: "", isTyping: false });

  send = async () => {
    const text = this.state.input.trim();
    if (!text || this.state.isStreaming) return;
    const provider = this.state.selectedProvider;
    const userMsg: Message = { role: "user", content: text, created_at: new Date().toISOString() };
    const base = [...this.state.messages, userMsg];
    const wasNew = !this.state.activeSessionId;
    this.setState({ messages: base, input: "", isStreaming: true, isTyping: true, streamText: "" }, this.scrollToBottom);
    this._abort = new AbortController();
    let sid = this.state.activeSessionId;
    let acc = "";
    try {
      await api.streamChat(
        { message: text, sessionId: sid, provider },
        {
          onStart: (newSid) => { if (!sid) { sid = newSid; this.setState({ activeSessionId: newSid }); } },
          onDelta: (t) => { acc += t; this.setState({ isTyping: false, streamText: acc }, this.scrollToBottom); },
        },
        this._abort.signal
      );
    } catch (e) {
      /* stream error or abort — keep partial text */
    } finally {
      const finalMsgs = [...base];
      if (acc.trim()) finalMsgs.push({ role: "assistant", content: acc, created_at: new Date().toISOString(), provider });
      if (sid) this.cache[sid] = finalMsgs;
      this.setState({ messages: finalMsgs, isStreaming: false, isTyping: false, streamText: "" }, this.scrollToBottom);
      this.syncSummary(sid, finalMsgs, wasNew);
    }
  };

  cancel = () => { if (this._abort) this._abort.abort(); };

  syncSummary(sid: string | null, msgs: Message[], _wasNew: boolean) {
    if (!sid) return;
    const firstUser = msgs.find((m) => m.role === "user");
    const now = new Date().toISOString();
    const convs = [...this.state.conversations];
    const idx = convs.findIndex((c) => c.session_id === sid);
    const summary: ConversationSummary = {
      session_id: sid,
      preview: (firstUser ? firstUser.content : "New conversation").slice(0, 90),
      message_count: msgs.length,
      created_at: idx >= 0 ? convs[idx].created_at : now,
      updated_at: now,
    };
    if (idx >= 0) convs.splice(idx, 1);
    this.setState({ conversations: [summary, ...convs] });
  }

  render() {
    const s = this.state;
    const isDark = s.theme === "dark";
    const busy = s.isStreaming || s.isTyping || !!s.streamText;
    const showEmpty = s.messages.length === 0 && !busy && !s.loadingConv;
    const activeModel = this.modelFor(s.selectedProvider);

    const sidebarStyle: React.CSSProperties = s.isMobile
      ? {
          position: "fixed", top: 0, left: 0, bottom: 0, zIndex: 40, width: "82%", maxWidth: 320,
          background: "var(--color-bg)", borderRight: "2px solid var(--color-divider)",
          display: "flex", flexDirection: "column",
          boxShadow: s.sidebarOpen ? "var(--shadow-lg)" : "none",
          transform: `translateX(${s.sidebarOpen ? "0" : "-102%"})`, transition: "transform .25s ease",
        }
      : {
          flex: "0 0 288px", width: 288, background: "var(--color-bg)",
          borderRight: "2px solid var(--color-divider)", display: "flex", flexDirection: "column", height: "100%",
        };

    const backdropStyle: React.CSSProperties =
      s.isMobile && s.sidebarOpen
        ? { position: "fixed", inset: 0, zIndex: 35, background: "color-mix(in srgb, var(--color-neutral-900) 55%, transparent)" }
        : { display: "none" };

    return (
      <div
        data-theme={s.theme}
        style={{ height: "100vh", width: "100%", display: "flex", overflow: "hidden", background: "var(--color-bg)", color: "var(--color-text)", fontFamily: "var(--font-body)" }}
      >
        <div style={backdropStyle} onClick={() => this.setState({ sidebarOpen: false })} />

        {/* ── Sidebar ── */}
        <aside style={sidebarStyle}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 16, borderBottom: "2px solid var(--color-divider)" }}>
            <div style={{ width: 14, height: 14, background: "var(--color-accent)" }} />
            <div style={{ fontFamily: "var(--font-heading)", fontWeight: 800, fontSize: 15, letterSpacing: "0.02em" }}>MODELINE</div>
            {s.isMobile && (
              <button className="btn btn-icon btn-secondary" style={{ marginLeft: "auto" }} aria-label="Close menu" onClick={() => this.setState({ sidebarOpen: false })}>
                <Icon d="M18 6L6 18M6 6l12 12" />
              </button>
            )}
          </div>
          <div style={{ padding: "12px 16px 4px" }}>
            <button className="btn btn-primary btn-block" onClick={this.newChat} style={{ justifyContent: "flex-start", marginTop: 0 }}>
              <Icon d="M12 5v14M5 12h14" size={16} /> New chat
            </button>
          </div>
          <div style={{ padding: "16px 16px 6px", fontSize: 11, letterSpacing: "0.1em", textTransform: "uppercase", color: muted(50) }}>Conversations</div>
          <div className="om-scroll" style={{ flex: 1, overflowY: "auto", padding: "0 10px 16px" }}>
            {s.loading && (
              <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 6px", color: muted(55), fontSize: 13 }}>
                <span style={{ width: 15, height: 15, border: "2px solid var(--color-neutral-400)", borderTopColor: "var(--color-accent)", animation: "om-spin .7s linear infinite", display: "inline-block" }} />
                Loading…
              </div>
            )}
            {!s.loading && s.conversations.length === 0 && (
              <div style={{ padding: "14px 6px", fontSize: 13, color: muted(55) }}>No conversations yet.</div>
            )}
            {s.conversations.map((c) => {
              const active = c.session_id === s.activeSessionId;
              return (
                <button
                  key={c.session_id}
                  onClick={() => this.selectConversation(c.session_id)}
                  aria-current={active ? "true" : undefined}
                  style={{
                    width: "100%", textAlign: "left", display: "block", border: 0, cursor: "pointer",
                    padding: "10px 12px", marginBottom: 2, font: "inherit", color: "inherit",
                    background: active ? "var(--color-surface)" : "transparent",
                    borderLeft: `2px solid ${active ? "var(--color-accent)" : "transparent"}`,
                  }}
                >
                  <div style={{ fontSize: 13, fontWeight: active ? 600 : 400, lineHeight: 1.35, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" } as any}>
                    {c.preview || "(empty)"}
                  </div>
                  <div style={{ fontSize: 11, marginTop: 3, color: muted(48) }}>{c.message_count} messages</div>
                </button>
              );
            })}
          </div>
        </aside>

        {/* ── Main ── */}
        <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", height: "100%" }}>
          <header className="nav" style={{ flex: "none", gap: 12 }}>
            {s.isMobile && (
              <button className="btn btn-icon btn-secondary" aria-label="Open menu" onClick={() => this.setState({ sidebarOpen: true })}>
                <Icon d="M3 6h18M3 12h18M3 18h18" />
              </button>
            )}
            <div className="nav-brand" style={{ fontSize: 16 }}>Chat</div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <label style={{ fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: muted(55) }} htmlFor="om-provider">Model</label>
              <select
                id="om-provider" className="input" aria-label="Model provider"
                value={s.selectedProvider}
                onChange={(e) => this.setState({ selectedProvider: e.target.value })}
                style={{ width: "auto", minWidth: 180, minHeight: 36 }}
              >
                {s.providers.map((p) => (
                  <option key={p.name} value={p.name}>{p.name} · {p.model}</option>
                ))}
              </select>
              <button className="btn btn-icon btn-secondary" aria-label="Toggle color theme" onClick={() => this.setState((st) => ({ theme: st.theme === "dark" ? "light" : "dark" }))}>
                {isDark ? (
                  <Icon d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1l2.1-2.1M17 7l2.1-2.1" extra={<circle cx="12" cy="12" r="4" />} />
                ) : (
                  <Icon d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
                )}
              </button>
            </div>
          </header>

          <div className="om-scroll" ref={this.setScrollRef} style={{ flex: 1, overflowY: "auto" }}>
            <div style={{ maxWidth: 820, margin: "0 auto", padding: "28px 24px 36px", display: "flex", flexDirection: "column", gap: 22 }}>
              {showEmpty && (
                <div style={{ minHeight: "52vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", textAlign: "center", gap: 14 }}>
                  <div style={{ width: 52, height: 52, display: "grid", placeItems: "center", background: "var(--color-surface)" }}>
                    <Icon d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" size={26} stroke="var(--color-accent)" />
                  </div>
                  <h3 style={{ margin: 0 }}>Start a conversation</h3>
                  <p className="text-muted" style={{ margin: 0, maxWidth: 380 }}>Ask anything. Replies stream in and render as markdown. Your chats appear in the sidebar.</p>
                </div>
              )}

              {s.messages.map((m, i) =>
                m.role === "user" ? (
                  <div key={i} style={{ alignSelf: "flex-end", maxWidth: "80%", display: "flex", flexDirection: "column", gap: 5, alignItems: "flex-end" }}>
                    <div style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase", color: muted(50) }}>You</div>
                    <div style={{ background: "var(--color-accent)", color: "var(--color-bg)", padding: "11px 15px", whiteSpace: "pre-wrap", lineHeight: 1.5, fontSize: 14 }}>{m.content}</div>
                  </div>
                ) : (
                  <div key={i} style={{ alignSelf: "flex-start", maxWidth: "100%", width: "100%", display: "flex", flexDirection: "column", gap: 6 }}>
                    {SHOW_MODEL_LABELS && (
                      <div style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--color-accent)" }}>{this.modelFor(m.provider)}</div>
                    )}
                    <div style={{ background: "var(--color-surface)", padding: "14px 18px", fontSize: 14, overflowWrap: "anywhere" }}>{parseMarkdown(m.content)}</div>
                  </div>
                )
              )}

              {s.isTyping && (
                <div style={{ alignSelf: "flex-start", display: "flex", flexDirection: "column", gap: 6 }}>
                  {SHOW_MODEL_LABELS && (
                    <div style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--color-accent)" }}>{activeModel}</div>
                  )}
                  <div style={{ display: "flex", gap: 6, padding: "14px 18px", background: "var(--color-surface)", width: "fit-content" }}>
                    {[0, 0.2, 0.4].map((d, i) => (
                      <span key={i} style={{ width: 6, height: 6, background: "var(--color-neutral-500)", animation: "om-blink 1.2s infinite ease-in-out", animationDelay: `${d}s` }} />
                    ))}
                  </div>
                </div>
              )}

              {!!s.streamText && (
                <div style={{ alignSelf: "flex-start", maxWidth: "100%", width: "100%", display: "flex", flexDirection: "column", gap: 6 }}>
                  {SHOW_MODEL_LABELS && (
                    <div style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--color-accent)" }}>{activeModel}</div>
                  )}
                  <div style={{ background: "var(--color-surface)", padding: "14px 18px", fontSize: 14, overflowWrap: "anywhere" }}>
                    {parseMarkdown(s.streamText)}
                    <span style={{ display: "inline-block", width: 7, height: 15, background: "var(--color-accent)", marginLeft: 2, verticalAlign: "text-bottom", animation: "om-caret 1s step-end infinite" }} />
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* ── Composer ── */}
          <div style={{ flex: "none", borderTop: "2px solid var(--color-divider)", padding: "14px 24px 18px" }}>
            <div style={{ maxWidth: 820, margin: "0 auto", display: "flex", gap: 10, alignItems: "flex-end" }}>
              <textarea
                className="input" rows={1} placeholder="Send a message…" aria-label="Message"
                value={s.input}
                onChange={(e) => this.setState({ input: e.target.value })}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.send(); } }}
                style={{ flex: 1, minHeight: 44, maxHeight: 180, resize: "none", lineHeight: 1.5, padding: "11px 12px" }}
              />
              {s.isStreaming ? (
                <button className="btn btn-primary" onClick={this.cancel} style={{ minHeight: 44, background: "var(--color-accent-700)" }} aria-label="Cancel streaming reply">
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" /></svg>
                  Cancel
                </button>
              ) : (
                <button className="btn btn-primary" onClick={this.send} disabled={!s.input.trim() || s.loading} style={{ minHeight: 44 }} aria-label="Send message">
                  <Icon d="M12 19V5M5 12l7-7 7 7" size={16} /> Send
                </button>
              )}
            </div>
          </div>
        </main>
      </div>
    );
  }
}

function Icon({ d, size = 18, stroke = "currentColor", extra }: { d: string; size?: number; stroke?: string; extra?: React.ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={stroke} strokeWidth={2} strokeLinecap="square">
      {extra}
      <path d={d} />
    </svg>
  );
}
