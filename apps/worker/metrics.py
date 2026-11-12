"""A worker's own metrics endpoint (M6 W6): the families of the runs this worker executes.

With the Redis backend, incidents run in workers, so the API's run-event families (tool calls,
model requests and their time, tokens, fixes, approval waits, recoveries) stay at 0 there. Each
worker counts its own runs with the API's trace sink (apps/api/metrics.py, ApiMetrics) and
serves them here:

    RECAP_METRICS=1                  on (off by default, like the API's)
    RECAP_WORKER_METRICS_PORT=9100   the port; unset, there is no endpoint
    RECAP_WORKER_METRICS_HOST        the address to listen on: 127.0.0.1 by default; Compose
                                     sets 0.0.0.0 inside its own network (never published)

GET /metrics answers in the Prometheus text format, without the HTTP families (a worker serves
no API) and without the queue's (the API reads those from Redis once for every process); any
other path answers 404. The labels are the API's closed sets: never an id, a prompt or text.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apps.api.metrics import PROMETHEUS_TEXT, ApiMetrics
from recap.core.errors import ConfigError

PORT_ENV = "RECAP_WORKER_METRICS_PORT"
HOST_ENV = "RECAP_WORKER_METRICS_HOST"


def address_from_environment() -> tuple[str, int] | None:
    """Where to serve: (host, port), or None when RECAP_WORKER_METRICS_PORT is unset."""
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError(f"{PORT_ENV} must be a port number, not {raw!r}") from None
    if not 0 < port < 65536:
        raise ConfigError(f"{PORT_ENV} must be a port number from 1 to 65535, not {port}")
    return os.environ.get(HOST_ENV, "").strip() or "127.0.0.1", port


def serve(metrics: ApiMetrics, host: str, port: int) -> ThreadingHTTPServer:
    """Answer GET /metrics from `metrics` on host:port, in a daemon thread (port 0: any free
    port, as server_address then says). shutdown() stops it."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = metrics.render(http=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", PROMETHEUS_TEXT)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return  # a scrape every few seconds is not worth a log line

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="worker-metrics", daemon=True).start()
    return server
