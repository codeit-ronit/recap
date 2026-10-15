import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5173. Requests to /api/* are proxied to the FastAPI backend on :8000
// with the "/api" prefix stripped (so /api/runs -> http://127.0.0.1:8000/runs).
// In production the built app is served by FastAPI itself, with API routes at the root.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  preview: {
    port: 4173,
  },
});
