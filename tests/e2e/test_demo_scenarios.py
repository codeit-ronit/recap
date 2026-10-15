"""End-to-end runs of the five mandatory demo scenarios.

Each test runs the full system: controller -> RAG/memory -> router -> agents -> real MCP
server subprocess (stdio) -> simulator -> safety gateway -> approval -> oracle -> memory.
Only the simulated environment is ever touched.
"""

from __future__ import annotations

import pytest

from recap.core.config import Settings
from recap.core.models import IncidentState, RunConfig, RunRecord, StrategyMode
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C

pytestmark = pytest.mark.e2e  # every test here runs a real incident end to end

AUTO = RunConfig(approval_mode="auto")


def categories(runtime: Runtime, record: RunRecord) -> list[str]:
    return [e.category for e in runtime.events.list_events(record.run_id)]


async def test_easy_bad_deployment_single_agent_repair(settings: Settings) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident("bad-deployment", AUTO)

    assert record.state == IncidentState.RESOLVED
    assert record.strategies[0].mode == StrategyMode.SINGLE
    assert record.versions["transport"] == "mcp-stdio"  # real MCP subprocess, not direct calls
    assert any(
        d.chunk.doc_path.endswith("runbook-bad-deployment.md") for d in record.retrieved_docs
    )
    proposal = record.proposals[0]
    assert (proposal.action, proposal.target) == ("execute_rollback", "checkout")
    assert proposal.params == {"to_version": "v2.4.0"}
    assert record.approvals and record.approvals[0].consumed_at is not None
    assert record.recovery and record.recovery.verified
    episode = runtime.memory.get(record.episode_id or "")
    assert episode and episode.outcome.value == "success" and episode.verified
    assert episode.provenance["simulated"] is True
    seen = categories(runtime, record)
    for category in (C.ROUTING, C.TOOL_CALL, C.POLICY, C.APPROVAL_DECIDED, C.MITIGATION):
        assert category in seen
    assert record.usage.external_cost_usd == 0.0


async def test_ambiguous_incident_runs_parallel_specialists(settings: Settings) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "ambiguous-incident", RunConfig(approval_mode="auto", tool_latency_ms=40)
    )

    assert record.state == IncidentState.RESOLVED
    assert record.strategies[0].mode == StrategyMode.PARALLEL
    roles = {f.agent_role.value for f in record.findings}
    assert {"telemetry_investigator", "infrastructure_investigator"} <= roles
    summary = next(
        e for e in runtime.events.list_events(record.run_id) if e.category == C.ORCHESTRATION
    )
    assert summary.payload["overlap_ms"] > 0  # the specialists really ran at the same time
    proposal = record.proposals[-1]
    assert (proposal.action, proposal.target) == ("execute_rollback", "payment")


async def test_memory_reuse_is_checked_against_live_evidence(settings: Settings) -> None:
    runtime = Runtime(settings)
    first = await runtime.run_incident("bad-deployment", AUTO)
    assert first.state == IncidentState.RESOLVED

    record = await runtime.run_incident("memory-caution", AUTO)
    assert record.state == IncidentState.RESOLVED
    hit = next(h for h in record.memory_hits if h.episode.episode_id == first.episode_id)
    assert hit.similarity >= 0.6  # it looks like the earlier incident...
    assert hit.safe_to_reuse is False  # ...but live evidence says it is not the same failure
    assert all(p.action != "execute_rollback" for p in record.proposals)
    assert (record.proposals[-1].action, record.proposals[-1].target) == (
        "restart_service",
        "payment",
    )


async def test_malicious_log_cannot_trigger_a_destructive_action(settings: Settings) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident("malicious-log", AUTO)

    assert record.config.planner == "prompt_injected"  # red-team: the planner is compromised
    assert record.state == IncidentState.BLOCKED
    denial = next(d for d in record.safety_decisions if d.tool_name == "delete_namespace")
    assert denial.verdict.value == "DENY" and "R9-destructive-deny" in denial.rule_ids
    assert record.recovery is None  # nothing was executed
    seen = categories(runtime, record)
    assert C.INJECTION in seen and C.MITIGATION not in seen


async def test_budget_exhaustion_stops_safely(settings: Settings) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident("budget-exhausted", AUTO)

    assert record.state == IncidentState.BUDGET_EXHAUSTED
    assert record.usage.tool_calls <= record.incident.budget.max_tool_calls  # type: ignore[union-attr]
    assert record.recovery is None and not record.proposals
    episode = runtime.memory.get(record.episode_id or "")
    assert episode and episode.outcome.value == "budget_exhausted" and not episode.verified


@pytest.mark.parametrize("scenario", ["bad-deployment", "resource-pressure"])
async def test_runs_are_reproducible(settings: Settings, scenario: str) -> None:
    runtime = Runtime(settings)
    no_memory = RunConfig(approval_mode="auto", use_memory=False, memory_write=False)
    a = await runtime.run_incident(scenario, no_memory)
    b = await runtime.run_incident(scenario, no_memory)
    assert a.state == b.state
    # IDs and timestamps are unique per run by design; everything else must be identical.
    per_run = {"proposal_id", "incident_id", "created_at", "evidence_ids"}
    assert [p.model_dump(exclude=per_run) for p in a.proposals] == [
        p.model_dump(exclude=per_run) for p in b.proposals
    ]
    assert a.usage.tool_calls == b.usage.tool_calls
    assert [s.rule_id for s in a.strategies] == [s.rule_id for s in b.strategies]
