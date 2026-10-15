"""The human-approval gate, exercised through the real controller and MCP subprocess.

A reversible action must not run before an exact approval exists; an approval for a
different proposal, a rejection, or an expired request must all stop the mitigation.
"""

from __future__ import annotations

import asyncio

import pytest

from recap.core.config import Settings
from recap.core.errors import ApprovalError
from recap.core.models import ApprovalRequest, IncidentState, RunConfig, RunRecord
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel
from recap.telemetry.tracer import TraceCategory as C
from recap.verification.oracle import EnvironmentHealthOracle

MANUAL = RunConfig(approval_mode="manual", memory_write=False)


async def start_and_wait_for_approval(
    runtime: Runtime, scenario: str = "bad-deployment"
) -> tuple[asyncio.Task[RunRecord], ApprovalRequest]:
    task = asyncio.create_task(
        runtime.run_incident(scenario, MANUAL, approval_channel=ExternalApprovalChannel())
    )
    for _ in range(400):
        pending = runtime.approvals.pending()
        if pending:
            return task, pending[0]
        if task.done():
            raise AssertionError(f"run ended before asking for approval: {task.result().state}")
        await asyncio.sleep(0.05)
    raise AssertionError("no approval request appeared")


def mitigation_events(runtime: Runtime, run_id: str) -> list[object]:
    return [e for e in runtime.events.list_events(run_id) if e.category == C.MITIGATION]


async def test_nothing_is_executed_before_approval_then_exact_approval_works(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    task, request = await start_and_wait_for_approval(runtime)

    # While waiting: no write happened and the simulated service is still broken.
    assert mitigation_events(runtime, request.run_id) == []
    sandbox = create_environment(runtime.settings.environment, request.run_id)
    assert (await EnvironmentHealthOracle(sandbox).probe()).healthy is False
    assert sandbox.store.load()["action_log"] == []  # type: ignore[attr-defined]
    # An approval naming a different action (wrong digest) is refused.
    with pytest.raises(ApprovalError):
        runtime.approvals.decide(
            request.proposal_id, decision="approved", proposal_digest="0" * 64, actor="tester"
        )
    runtime.approvals.decide(
        request.proposal_id,
        decision="approved",
        proposal_digest=request.proposal_digest,
        actor="tester",
    )
    record = await asyncio.wait_for(task, 60)
    assert record.state == IncidentState.RESOLVED
    assert len(mitigation_events(runtime, record.run_id)) == 1
    # The approval was single-use: it is consumed and cannot authorize anything again.
    assert record.approvals[0].consumed_at is not None


async def test_rejection_blocks_the_mitigation(settings: Settings) -> None:
    runtime = Runtime(settings)
    task, request = await start_and_wait_for_approval(runtime)
    runtime.approvals.decide(
        request.proposal_id,
        decision="rejected",
        proposal_digest=request.proposal_digest,
        actor="tester",
    )
    record = await asyncio.wait_for(task, 60)
    assert record.state == IncidentState.BLOCKED
    assert mitigation_events(runtime, record.run_id) == []
    assert record.recovery is None


async def test_expired_approval_blocks_the_mitigation(settings: Settings) -> None:
    budget = settings.budget.model_copy(
        update={"approvals": settings.budget.approvals.model_copy(update={"ttl_seconds": 1})}
    )
    runtime = Runtime(settings.model_copy(update={"budget": budget}))
    task, request = await start_and_wait_for_approval(runtime)
    record = await asyncio.wait_for(task, 60)  # nobody answers
    assert record.state == IncidentState.BLOCKED
    assert mitigation_events(runtime, record.run_id) == []
    with pytest.raises(ApprovalError):  # a late approval is refused
        runtime.approvals.decide(
            request.proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor="late",
        )
