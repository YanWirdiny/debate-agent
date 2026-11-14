import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev: the backend runs on :8000 (uvicorn app.main:app). Everything except the SPA is proxied.
const API = process.env.API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/ws": { target: API.replace(/^http/, "ws"), ws: true },
      "/clips": API,
      "/sessions": API,
      "/state": API,
      "/health": API,
      "^/report/.*": API, // regex: a plain "/report" prefix would also catch /reports. Backend JSON + printable HTML; the SPA's report page is /reports/:id
    },
  },
});
