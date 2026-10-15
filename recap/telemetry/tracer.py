"""Tracer: records every important step of a run as a structured TraceEvent.

All layers call `tracer.emit(...)`. The tracer redacts secrets, numbers events in
order, and hands each one to every configured sink (JSONL file, SQLite, live stream
for the dashboard, optional OpenTelemetry). The resulting trace is the run's audit log.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from recap.core.ids import new_id, utcnow
from recap.core.interfaces import TraceSink
from recap.core.models import TraceEvent, Usage

log = logging.getLogger(__name__)


class TraceCategory:
    """Event categories used across RECAP (one place, so the dashboard can rely on them)."""

    STATE = "state.transition"
    ROUTING = "routing.decision"
    RETRIEVAL = "retrieval.docs"
    RETRIEVAL_SKIP = "retrieval.skip"
    MEMORY_SEARCH = "memory.search"
    MEMORY_ASSESS = "memory.assessment"
    MEMORY_WRITE = "memory.consolidated"
    MEMORY_SKIP = "memory.skip"
    CONTEXT = "context.pack"
    INVESTIGATION_ROUND = "investigation.round"
    ORCHESTRATION = "orchestration.summary"
    AGENT_START = "agent.start"
    AGENT_DECISION = "agent.decision"
    AGENT_FINISH = "agent.finish"
    TOOL_DISCOVERY = "tool.discovery"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    POLICY = "policy.decision"
    INJECTION = "safety.injection_flag"
    PROPOSAL = "mitigation.proposed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    MITIGATION = "mitigation.executed"
    VERIFY_EVIDENCE = "verification.evidence"
    VERIFY_RECOVERY = "verification.recovery"
    BUDGET = "budget.exhausted"
    RUN_ERROR = "run.error"
    RUN_SUMMARY = "run.summary"


class MemorySink:
    """Keeps events in a Python list. Handy in tests and scripts that inspect one run."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def write(self, event: TraceEvent) -> None:
        self.events.append(event)


class Tracer:
    """Emits ordered, redacted TraceEvents for one run."""

    def __init__(
        self,
        run_id: str,
        sinks: Sequence[TraceSink] = (),
        redact: Callable[[Any], Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.sinks: list[TraceSink] = list(sinks)
        self.stage = "CREATED"  # the controller updates this on every state transition
        self._redact = redact or (lambda value: value)
        self._seq = 0

    def emit(
        self,
        category: str,
        payload: dict[str, Any] | None = None,
        *,
        agent: str | None = None,
        duration_ms: float | None = None,
        usage: Usage | None = None,
    ) -> TraceEvent:
        """Create one event, redact it, and write it to all sinks."""
        self._seq += 1
        event = TraceEvent(
            event_id=new_id("evt"),
            run_id=self.run_id,
            seq=self._seq,
            stage=self.stage,
            ts=utcnow(),
            agent=agent,
            category=category,
            payload=self._redact(payload or {}),
            duration_ms=duration_ms,
            usage=usage,
        )
        for sink in self.sinks:
            try:
                sink.write(event)
            except Exception:  # a broken sink must never break an incident run
                log.exception("trace sink %r failed", sink)
        return event
