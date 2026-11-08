"""Deterministic crash injection at real execution boundaries, for the crash tests only.

A worker started with RECAP_FAULT_INJECTION=1 and RECAP_FAULT_POINT=<point> kills its own
process with SIGKILL, with no cleanup and no `finally` (what an out-of-memory kill or a power cut
does), the first time its run reaches that point:

    after_claim        the job is claimed and marked running; the run has not started
    investigating      the first agent started (agent.start is persisted)
    awaiting_approval  the approval request is persisted (approval.requested)
    before_write       the write-ahead record is persisted (mitigation.started); nothing was sent
    after_write        the target applied the write and answered; the outcome is not recorded yet
    verifying          the run entered VERIFYING (the fix's outcome is recorded)

The points tied to a trace event fire from a trace sink that runs after the SQLite sink, so the
event that marks the point is persisted, exactly as in a real crash right after it. after_write
fires inside the tool transport, after the target's answer arrives and before the gateway can
record it. Without RECAP_FAULT_INJECTION=1 nothing in this module is ever active.
"""

from __future__ import annotations

import os
import signal
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

from recap.core.errors import ConfigError
from recap.core.models import TraceEvent

POINTS = (
    "after_claim",
    "investigating",
    "awaiting_approval",
    "before_write",
    "after_write",
    "verifying",
)

# point -> (trace category, required payload["to"] or None)
_EVENT_POINTS = {
    "investigating": ("agent.start", None),
    "awaiting_approval": ("approval.requested", None),
    "before_write": ("mitigation.started", None),
    "verifying": ("state.transition", "VERIFYING"),
}


def crash() -> NoReturn:
    """Die at once, like a killed process: no exception handlers, no cleanup."""
    os.kill(os.getpid(), signal.SIGKILL)
    raise SystemExit(137)  # unreachable: SIGKILL cannot be caught


@dataclass(frozen=True)
class FaultPlan:
    point: str

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> FaultPlan | None:
        env = os.environ if env is None else env
        if env.get("RECAP_FAULT_INJECTION", "").strip() != "1":
            return None
        point = env.get("RECAP_FAULT_POINT", "").strip()
        if point not in POINTS:
            raise ConfigError(
                f"RECAP_FAULT_POINT must be one of {', '.join(POINTS)}, not {point!r}"
            )
        return cls(point)

    def sink(self) -> FaultSink | None:
        return FaultSink(self) if self.point in _EVENT_POINTS else None

    def at(self, point: str) -> None:
        if self.point == point:
            crash()


class FaultSink:
    """A trace sink that crashes the process at the plan's event (after SQLite stored it)."""

    def __init__(self, plan: FaultPlan) -> None:
        self.category, self.to = _EVENT_POINTS[plan.point]

    def write(self, event: TraceEvent) -> None:
        if event.category == self.category and (
            self.to is None or event.payload.get("to") == self.to
        ):
            crash()


class _CrashAfterWrite:
    """A tool transport that dies right after a write tool's answer comes back."""

    def __init__(self, inner: Any, write_tools: Collection[str]) -> None:
        self.inner = inner
        self.write_tools = frozenset(write_tools)
        self.transport_name = getattr(inner, "transport_name", "mcp")

    async def list_tools(self) -> Any:
        return await self.inner.list_tools()

    async def call_tool(self, name: str, args: dict[str, object], timeout_seconds: float) -> Any:
        result = await self.inner.call_tool(name, args, timeout_seconds)
        if name in self.write_tools:
            crash()
        return result


def crash_after_write(
    inner_factory: Callable[[Any, str], AbstractAsyncContextManager[Any]],
    write_tools: Collection[str],
) -> Callable[[Any, str], AbstractAsyncContextManager[Any]]:
    """Wrap a transport factory so the worker dies once the target has applied a write."""

    @asynccontextmanager
    async def factory(environment: Any, run_id: str) -> AsyncIterator[Any]:
        async with inner_factory(environment, run_id) as transport:
            yield _CrashAfterWrite(transport, write_tools)

    return factory
