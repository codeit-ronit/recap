"""Optional OpenTelemetry bridge: mirrors trace events as OTel spans printed to the console.

V1's real trace store is local (JSONL + SQLite, see sinks.py). This bridge only shows how
RECAP events map onto OpenTelemetry spans: each event becomes one span, written to stderr by
the SDK's ConsoleSpanExporter. There is no collector and nothing leaves the machine, so this
is NOT distributed tracing. Turn it on with RECAP_ENABLE_OTEL=1 after
`pip install -e '.[otel]'`; the composition root (recap/runtime.py) calls `maybe_otel_sink`.
"""

from __future__ import annotations

import sys

from recap.core.interfaces import TraceSink
from recap.core.models import TraceEvent

INSTALL_HINT = "OpenTelemetry is optional: pip install -e '.[otel]'"


class OTelTraceSink:
    """Turns each TraceEvent into one OpenTelemetry span printed by a console exporter."""

    def __init__(self) -> None:
        try:
            # Imported here, not at the top: the SDK is an optional extra.
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
        except ImportError as exc:
            raise RuntimeError(INSTALL_HINT) from exc
        # A private provider (not the global one) never changes OTel settings of a host app.
        # stderr keeps stdout clean for CLI reports; the simple processor exports each span
        # at once, without a background thread.
        provider = TracerProvider(resource=Resource.create({"service.name": "recap"}))
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
        self._tracer = provider.get_tracer("recap")

    def write(self, event: TraceEvent) -> None:
        """Emit one span named after the event category and end it immediately."""
        attributes = {
            "run_id": event.run_id,
            "stage": event.stage,
            "agent": event.agent,
            "seq": event.seq,
            "duration_ms": event.duration_ms,
        }
        # A timed event is emitted when its block finishes, so its span covers the measured
        # duration up to the event time; other events become zero-length marker spans.
        end_ns = int(event.ts.timestamp() * 1e9)
        start_ns = end_ns - int((event.duration_ms or 0.0) * 1e6)
        span = self._tracer.start_span(
            event.category,
            # OpenTelemetry rejects None attribute values, so unset fields are left out.
            attributes={name: value for name, value in attributes.items() if value is not None},
            start_time=start_ns,
        )
        span.end(end_time=end_ns)


def maybe_otel_sink(enabled: bool) -> TraceSink | None:
    """The console OTel bridge when the feature flag is on, otherwise None."""
    return OTelTraceSink() if enabled else None
