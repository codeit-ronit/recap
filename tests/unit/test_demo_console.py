"""scripts/demo.py's console lines: what a person watching a live run needs to see."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from recap.core.models import TraceEvent
from recap.telemetry.tracer import TraceCategory
from scripts import demo

TIMED_OUT = "agent crashed: ProviderError: 127.0.0.1:11435 did not answer within 300s"


def parallel_round(*agents: dict[str, Any]) -> TraceEvent:
    return TraceEvent(
        event_id="evt-1",
        run_id="run-1",
        seq=1,
        stage="INVESTIGATING",
        ts=datetime.now(UTC),
        category=TraceCategory.ORCHESTRATION,
        payload={"mode": "parallel", "overlap_ms": 300393.0, "wall_ms": 595389.0, "agents": agents},
    )


def test_a_parallel_round_names_the_agent_that_crashed() -> None:
    """A model request that timed out on the owner's VM left the telemetry agent without
    findings (M5): the line before the verifier's "insufficient" has to say why."""
    event = parallel_round(
        {"role": "telemetry_investigator", "error": TIMED_OUT},
        {"role": "infrastructure_investigator", "error": None},
    )
    assert demo.ConsoleSink()._format(event) == (
        "PARALLEL  agents overlapped for 300393 ms (wall 595389 ms)\n"
        f"          ✗ telemetry_investigator: {TIMED_OUT}"
    )


def test_a_parallel_round_without_a_crash_stays_one_line() -> None:
    event = parallel_round(
        {"role": "telemetry_investigator"}, {"role": "infrastructure_investigator"}
    )
    assert demo.ConsoleSink()._format(event) == (
        "PARALLEL  agents overlapped for 300393 ms (wall 595389 ms)"
    )
