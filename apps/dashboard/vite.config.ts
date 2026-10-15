import { defineConfig } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on :5173. Requests to /api/* are proxied to the FastAPI backend on :8000
// with the "/api" prefix stripped (so /api/runs -> http://127.0.0.1:8000/runs).
// In production the built app is served by FastAPI itself, with API routes at the root.

/** Host names the dev server answers to: this machine only (see apps/api/auth.py). */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * Refuse requests addressed to any other host name. A web page the developer visits could
 * otherwise use DNS rebinding to read the dev server's files and, through the proxy, the API.
 * (Vite 5.4.10 has no `server.allowedHosts` option yet.)
 */
function localHostsOnly(): Plugin {
  return {
    name: "recap-local-hosts-only",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const host = (req.headers.host ?? "").replace(/:\d+$/, "").toLowerCase();
        if (LOCAL_HOSTS.has(host)) return next();
        res.statusCode = 403;
        res.end("RECAP dev server: requests for other host names are refused");
      });
    },
  };
}

export default defineConfig({
  plugins: [localHostsOnly(), react()],
  server: {
    port: 5173,
    cors: false, // the dashboard calls the API through the same-origin proxy; no CORS needed
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        // Keep the browser's Host header, so the API's own trusted-host check still applies.
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  preview: {
    port: 4173,
  },
});
