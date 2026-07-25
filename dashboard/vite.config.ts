import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard reads telemetry, owned by the INGESTION service (:8001). Dev
// server proxies the read endpoints there so the app calls same-origin
// relative URLs — no CORS.
const BACKEND = "http://localhost:8001";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/stats": BACKEND, // covers /stats, /stats/timeseries, /stats/by_model
      "/logs": BACKEND,
      "/hello": BACKEND,
    },
  },
});
