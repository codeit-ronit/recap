"""Fault injection (M6 W6): a cancellation the existing fault tests did not reach.

Found while mapping W6's fault cases, and fixed: ToolGateway._send_write absorbed only the
first cancellation, so a second one cancelled the write in flight, no outcome followed
mitigation.started, and the record said the fix "was not sent". Every wait is shielded now.

A cancelled run waits for the outcome of a write it already sent (ToolGateway._send_write: a
write that reached the target cannot be called back), records it, and only then stops;
tests/integration/test_controller_paths.py checks this for one cancellation. A second
cancellation can arrive while the run waits: a second POST /runs/{id}/cancel (RunManager.cancel
cancels the run's task again until it is done), or a worker fencing itself off at every
heartbeat Redis does not answer (Worker._fence). The real controller, the scripted policy, the
simulator and the MCP server subprocess run here, as in the controller tests.
"""

from __future__ import annotations

import asyncio

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C

pytestmark = pytest.mark.e2e  # a real incident through the controller and the MCP subprocess

WRITE_LATENCY_MS = 1500  # the simulator applies the write this long after it arrives


async def wait_for_event(
    runtime: Runtime,
    task: asyncio.Task,  # type: ignore[type-arg]
    run_id: str,
    category: C,
) -> None:
    for _ in range(500):
        if any(event.category == category for event in runtime.events.list_events(run_id)):
            return
        assert not task.done(), f"run ended before a {category.value} event"
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {category.value} event appeared")


async def test_a_second_cancel_while_a_fix_is_being_sent_still_records_that_fix(
    settings: Settings,
) -> None:
    """Cancelled twice while the approved write is in flight: the run still waits for the
    write's outcome and records it once (ok, run_cancelled), its record says what the tool
    reported, and the simulator applied the write exactly once, as after a single cancel."""
    options = dict(settings.environment.options, write_latency_ms=WRITE_LATENCY_MS)
    environment = settings.environment.model_copy(update={"options": options})
    runtime = Runtime(settings.model_copy(update={"environment": environment}))
    run_id = "run-cancel-twice"
    task = asyncio.create_task(
        runtime.run_incident(
            "bad-deployment", RunConfig(approval_mode="auto", memory_write=False), run_id=run_id
        )
    )
    await wait_for_event(runtime, task, run_id, C.MITIGATION_STARTED)
    task.cancel()  # the gateway now waits for the outcome of the write in flight
    await asyncio.sleep(0.2)  # well inside the write's 1.5 s
    task.cancel()  # a second cancel: another click, or the worker's next fence
    with pytest.raises(asyncio.CancelledError):
        await task

    record = runtime.runs.get(run_id)
    assert record is not None and record.state == IncidentState.FAILED
    assert record.outcome_note == "run cancelled"
    events = runtime.events.list_events(run_id)
    assert len([e for e in events if e.category == C.MITIGATION_STARTED]) == 1
    sent = [e.payload for e in events if e.category == C.MITIGATION]
    assert [(p["ok"], p["run_cancelled"]) for p in sent] == [(True, True)]
    [fix] = record.actions
    assert "the tool reported it done" in fix.note and "not sent" not in fix.note
    sandbox = create_environment(runtime.settings.environment, run_id)
    assert len(sandbox.store.load()["action_log"]) == 1  # type: ignore[attr-defined]
