import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI app serves the built bundle from /app/static with the hashed
// JS/CSS under /assets (StaticFiles mount in job_hunter/webapi.py). Vite's
// default base "/" + default outDir "dist" + assetsDir "assets" already match,
// so no base override is needed. During `npm run dev`, /api is proxied to the
// FastAPI dev server (uvicorn job_hunter.webapi:app) for live data + auth.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/login": { target: "http://localhost:8000", changeOrigin: true },
      "/auth": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
