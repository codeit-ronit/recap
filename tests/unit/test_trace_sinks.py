"""Tests for the trace sinks (JSONL, SQLite, live broadcast) and the optional OTel bridge."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from recap.core.models import TraceEvent
from recap.telemetry.otel import OTelTraceSink, maybe_otel_sink
from recap.telemetry.sinks import BroadcastHub, BroadcastSink, JsonlTraceSink, SqliteTraceSink
from recap.telemetry.tracer import TraceCategory, Tracer

SDK_MODULES = (
    "opentelemetry.sdk",
    "opentelemetry.sdk.resources",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
)


def emit_run(tracer: Tracer) -> list[TraceEvent]:
    """Emit three typical events and return them."""
    return [
        tracer.emit(TraceCategory.STATE, {"from": "CREATED", "to": "RETRIEVING"}),
        tracer.emit(
            TraceCategory.TOOL_RESULT,
            {"tool": "get_service_logs", "ok": True},
            agent="investigator",
            duration_ms=12.5,
        ),
        tracer.emit(TraceCategory.RUN_SUMMARY, {"state": "RESOLVED"}),
    ]


# --- JSONL ----------------------------------------------------------------------------------


def test_jsonl_sink_round_trips_events(tmp_path: Path) -> None:
    sink = JsonlTraceSink(tmp_path / "traces")
    events = emit_run(Tracer("run-abc123", sinks=[sink]))

    path = sink.path_for("run-abc123")
    assert path == tmp_path / "traces" / "run-abc123.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [TraceEvent.model_validate_json(line) for line in lines] == events


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", ".hidden"])
def test_jsonl_sink_refuses_run_ids_that_are_not_plain_file_names(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ValueError, match="run_id"):
        JsonlTraceSink(tmp_path).path_for(run_id)


# --- SQLite ---------------------------------------------------------------------------------


def test_sqlite_sink_lists_a_runs_events_in_order_after_a_sequence_number(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "recap.db"
    sink = SqliteTraceSink(db_path)
    run_a = emit_run(Tracer("run-a", sinks=[sink]))
    emit_run(Tracer("run-b", sinks=[sink]))  # another run shares the same table

    assert sink.list_events("run-a") == run_a
    assert [event.seq for event in sink.list_events("run-a", after_seq=1)] == [2, 3]
    assert [event.seq for event in sink.list_events("run-a", after_seq=1, limit=1)] == [2]
    assert sink.list_events("run-unknown") == []
    # A second sink on the same file sees the stored trace (that is how the API reads it).
    assert SqliteTraceSink(db_path).list_events("run-a") == run_a


def test_sqlite_sink_ignores_a_repeated_event(tmp_path: Path) -> None:
    sink = SqliteTraceSink(tmp_path / "recap.db")
    event = Tracer("run-a").emit(TraceCategory.STATE, {"to": "RETRIEVING"})

    sink.write(event)
    sink.write(event)

    assert sink.list_events("run-a") == [event]


# --- live broadcast -------------------------------------------------------------------------


async def test_broadcast_reaches_only_subscribers_of_the_same_run() -> None:
    hub = BroadcastHub()
    mine, other = hub.subscribe("run-a"), hub.subscribe("run-b")
    tracer = Tracer("run-a", sinks=[BroadcastSink(hub)])

    event = tracer.emit(TraceCategory.STATE, {"to": "RETRIEVING"})

    assert await asyncio.wait_for(mine.get(), timeout=1) == event
    assert other.empty()

    hub.unsubscribe("run-a", mine)
    tracer.emit(TraceCategory.STATE, {"to": "INVESTIGATING"})
    assert mine.empty()
    hub.unsubscribe("run-a", mine)  # unsubscribing twice is harmless


async def test_broadcast_drops_new_events_for_a_full_queue_instead_of_blocking() -> None:
    hub = BroadcastHub()
    queue = hub.subscribe("run-a")
    tracer = Tracer("run-a")  # no sinks: we publish by hand so any error would surface here

    for _ in range(queue.maxsize + 5):
        hub.publish(tracer.emit(TraceCategory.TOOL_CALL, {}))

    assert queue.qsize() == queue.maxsize == 1000
    assert (await queue.get()).seq == 1  # what was already buffered is kept


# --- optional OpenTelemetry bridge ----------------------------------------------------------


def test_otel_sink_is_off_unless_enabled() -> None:
    assert maybe_otel_sink(False) is None


def test_otel_sink_explains_how_to_install_the_optional_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in SDK_MODULES:  # a None entry makes "import <name>" fail, as if not installed
        monkeypatch.setitem(sys.modules, name, None)

    with pytest.raises(RuntimeError, match=r"pip install -e '\.\[otel\]'"):
        OTelTraceSink()
    with pytest.raises(RuntimeError, match="OpenTelemetry is optional"):
        maybe_otel_sink(True)


def stub_module(name: str, **attributes: Any) -> types.ModuleType:
    """A stand-in module holding `attributes` (replaces an optional package in a test)."""
    module = types.ModuleType(name)
    vars(module).update(attributes)
    return module


def test_otel_sink_maps_an_event_to_one_ended_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs without the real SDK: tiny stand-ins record the span the sink creates."""
    spans: list[dict[str, Any]] = []
    resources: list[dict[str, str]] = []

    class StubSpan:
        def __init__(self, record: dict[str, Any]) -> None:
            self.record = record

        def end(self, end_time: int | None = None) -> None:
            self.record["end_time"] = end_time

    class StubTracer:
        def start_span(self, name: str, attributes: Any = None, start_time: Any = None) -> Any:
            spans.append({"name": name, "attributes": attributes, "start_time": start_time})
            return StubSpan(spans[-1])

    class StubProvider:
        def __init__(self, resource: dict[str, str]) -> None:
            resources.append(resource)

        def add_span_processor(self, processor: Any) -> None: ...

        def get_tracer(self, name: str) -> StubTracer:
            return StubTracer()

    stubs = [
        stub_module("opentelemetry.sdk"),
        stub_module("opentelemetry.sdk.resources", Resource=types.SimpleNamespace(create=dict)),
        stub_module("opentelemetry.sdk.trace", TracerProvider=StubProvider),
        stub_module(
            "opentelemetry.sdk.trace.export",
            ConsoleSpanExporter=lambda out: None,
            SimpleSpanProcessor=lambda exporter: None,
        ),
    ]
    for module in stubs:
        monkeypatch.setitem(sys.modules, module.__name__, module)

    event = Tracer("run-a").emit(TraceCategory.TOOL_RESULT, {}, duration_ms=250.0)
    OTelTraceSink().write(event)

    assert resources == [{"service.name": "recap"}]
    [span] = spans
    assert span["name"] == "tool.result"
    # agent is None here, and OpenTelemetry rejects None values, so it is left out.
    assert span["attributes"] == {
        "run_id": "run-a",
        "stage": "CREATED",
        "seq": 1,
        "duration_ms": 250.0,
    }
    assert span["end_time"] - span["start_time"] == 250_000_000  # nanoseconds = 250 ms


def test_otel_sink_prints_spans_with_the_real_sdk(capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip(
        "opentelemetry.sdk.trace", reason="optional extra: pip install -e '.[otel]'"
    )
    sink = OTelTraceSink()
    event = Tracer("run-a").emit(TraceCategory.TOOL_CALL, {}, agent="investigator")

    sink.write(event)

    printed = capsys.readouterr().err
    assert "tool.call" in printed
    assert "run-a" in printed
