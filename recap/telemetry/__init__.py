"""Telemetry: structured trace events, where they are stored, and honest usage labels.

`tracer.py` emits numbered, redacted TraceEvents; `sinks.py` writes them to JSONL, SQLite
and a live in-process stream; `redact.py` removes secrets; `usage.py` labels cost without
mixing estimates and billed numbers; `otel.py` is an optional OpenTelemetry console bridge.
"""
