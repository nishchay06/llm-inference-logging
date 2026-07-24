// Real backend client — same shape as the design's mock `api`, so the ported
// component is unchanged. Relative URLs go through Vite's dev proxy to the
// FastAPI backend (:8000); in a production build they're same-origin.

export type Provider = { name: string; model: string };
export type ProvidersResponse = { providers: Provider[]; default: string };
export type ConversationSummary = {
  session_id: string;
  preview: string;
  message_count: number;
  created_at: string;
  updated_at: string;
};
export type Message = {
  role: "user" | "assistant";
  content: string;
  created_at?: string;
  provider?: string;
};
export type ConversationDetail = {
  session_id: string;
  created_at: string;
  updated_at: string;
  messages: Message[];
};

type StreamInput = { message: string; sessionId: string | null; provider: string };
type StreamHandlers = { onStart: (sessionId: string) => void; onDelta: (text: string) => void };

export const api = {
  async listProviders(): Promise<ProvidersResponse> {
    const r = await fetch("/providers");
    return r.json();
  },

  async listConversations(): Promise<ConversationSummary[]> {
    const r = await fetch("/conversations");
    return r.json();
  },

  async getConversation(sessionId: string): Promise<ConversationDetail> {
    const r = await fetch("/conversations/" + encodeURIComponent(sessionId));
    return r.json();
  },

  // POST /chat/stream returns SSE frames: {type:"start",session_id}, {type:"delta",text},
  // {type:"done"}, {type:"error",message}. We tee deltas to the caller.
  async streamChat(input: StreamInput, handlers: StreamHandlers, signal: AbortSignal): Promise<void> {
    const resp = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: input.message,
        session_id: input.sessionId,
        provider: input.provider,
      }),
      signal,
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);
        if (!frame.startsWith("data:")) continue;
        const evt = JSON.parse(frame.slice(5).trim());
        if (evt.type === "start") handlers.onStart(evt.session_id);
        else if (evt.type === "delta") handlers.onDelta(evt.text);
        else if (evt.type === "error") throw new Error(evt.message || "stream error");
      }
    }
  },
};
