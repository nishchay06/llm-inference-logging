import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the chat API to the FastAPI backend (Docker chatbot on
// :8000), so the React app calls same-origin relative URLs — no CORS, and SSE
// streaming flows through. Change the target if your backend runs elsewhere.
const BACKEND = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/providers": BACKEND,
      "/conversations": BACKEND,
      "/chat": BACKEND, // covers /chat and /chat/stream
      "/hello": BACKEND,
    },
  },
});
