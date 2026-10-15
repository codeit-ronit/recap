"""Tests for the Orchestrator: real concurrency, timeouts, crashes, budget and the trace event.

The agents here are fakes that sleep or fail on purpose, so timing and error handling can be
checked without a provider, MCP or a simulator.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from pydantic import ValidationError

from recap.core.config import AgentSpec
from recap.core.errors import BudgetExhausted, ConfigError
from recap.core.ids import utcnow
from recap.core.models import (
    AgentFinding,
    AgentRole,
    RoutingSignals,
    StrategyDecision,
    StrategyMode,
)
from recap.routing.orchestrator import AgentRunPlan, Orchestrator
from recap.telemetry.tracer import MemorySink, TraceCategory, Tracer

TELEMETRY, INFRA = AgentRole.TELEMETRY, AgentRole.INFRASTRUCTURE
INVESTIGATOR = AgentRole.INVESTIGATOR
SPECS = {role: AgentSpec(role=role, goal="investigate", max_steps=8) for role in AgentRole}
SIGNALS = RoutingSignals(
    services_affected=2, remaining_tool_calls=30, remaining_steps=30, max_parallel_agents=2
)
Behaviour = Callable[[], Awaitable[None]]


def sleep_for(seconds: float) -> Behaviour:
    async def behave() -> None:
        await asyncio.sleep(seconds)

    return behave


def fail_with(error: Exception) -> Behaviour:
    async def behave() -> None:
        raise error

    return behave


class FakeAgents:
    """A runner whose agents behave as told and remember their plans and finishing order."""

    def __init__(self, behaviours: dict[AgentRole, Behaviour]) -> None:
        self.behaviours = behaviours
        self.plans: list[AgentRunPlan] = []
        self.finished: list[AgentRole] = []

    async def __call__(self, spec: AgentSpec, plan: AgentRunPlan) -> AgentFinding:
        self.plans.append(plan)
        started = utcnow()
        await self.behaviours.get(plan.role, sleep_for(0))()
        self.finished.append(plan.role)
        return AgentFinding(
            agent_role=spec.role,
            summary=f"{plan.role.value} done",
            started_at=started,
            finished_at=utcnow(),
            round=plan.round,
        )


def decision(
    agents: Sequence[AgentRole] = (TELEMETRY, INFRA),
    mode: StrategyMode = StrategyMode.PARALLEL,
    budget: dict[str, int] | None = None,
    round_number: int = 1,
) -> StrategyDecision:
    return StrategyDecision(
        mode=mode,
        reason="test",
        rule_id="RT-test",
        selected_agents=list(agents),
        provider="",
        budget=budget or {},
        signals=SIGNALS,
        round=round_number,
    )


def recording_tracer() -> Tracer:
    """A tracer whose events can be read back from its MemorySink."""
    return Tracer("run-test", [MemorySink()])


def orchestration_payload(tracer: Tracer) -> dict[str, Any]:
    events = tracer.sinks[0].events  # type: ignore[attr-defined]
    return next(e.payload for e in events if e.category == TraceCategory.ORCHESTRATION)


async def test_parallel_agents_really_run_at_the_same_time() -> None:
    agents = FakeAgents({TELEMETRY: sleep_for(0.2), INFRA: sleep_for(0.2)})
    tracer = recording_tracer()
    started = time.perf_counter()
    findings = await Orchestrator(agents, SPECS, tracer).execute(decision())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.35  # one after another, two 0.2 s agents would need 0.4 s
    payload = orchestration_payload(tracer)
    assert payload["mode"] == "parallel" and payload["overlap_ms"] > 0
    assert payload["sum_ms"] > payload["wall_ms"]
    assert [entry["role"] for entry in payload["agents"]] == [TELEMETRY.value, INFRA.value]
    assert all(entry["error"] is None for entry in payload["agents"])
    assert [finding.agent_role for finding in findings] == [TELEMETRY, INFRA]


async def test_a_single_agent_has_no_overlap() -> None:
    tracer = recording_tracer()
    agents = FakeAgents({INVESTIGATOR: sleep_for(0.05)})
    single = decision([INVESTIGATOR], StrategyMode.SINGLE)
    findings = await Orchestrator(agents, SPECS, tracer).execute(single)

    assert [finding.summary for finding in findings] == ["investigator done"]
    payload = orchestration_payload(tracer)
    assert payload["mode"] == "single" and payload["overlap_ms"] == 0


async def test_a_timeout_becomes_an_error_finding_and_the_other_agent_survives() -> None:
    tracer = recording_tracer()
    agents = FakeAgents({TELEMETRY: sleep_for(5), INFRA: sleep_for(0.01)})
    orchestrator = Orchestrator(agents, SPECS, tracer, agent_timeout_seconds=0.1)
    telemetry, infrastructure = await orchestrator.execute(decision())

    assert telemetry.error == "timeout after 0.1s" and telemetry.agent_role == TELEMETRY
    assert telemetry.finished_at >= telemetry.started_at
    assert (
        infrastructure.error is None
        and infrastructure.summary == "infrastructure_investigator done"
    )
    assert orchestration_payload(tracer)["agents"][0]["error"] == "timeout after 0.1s"


async def test_a_crash_becomes_an_error_finding() -> None:
    agents = FakeAgents({TELEMETRY: fail_with(ValueError("boom"))})
    telemetry, infrastructure = await Orchestrator(agents, SPECS, Tracer("run-test")).execute(
        decision()
    )

    assert telemetry.error == "agent crashed: ValueError: boom"
    assert infrastructure.error is None


async def test_budget_exhaustion_is_raised_only_after_every_agent_stopped() -> None:
    tracer = recording_tracer()
    agents = FakeAgents(
        {TELEMETRY: fail_with(BudgetExhausted("max_tool_calls")), INFRA: sleep_for(0.1)}
    )

    with pytest.raises(BudgetExhausted):
        await Orchestrator(agents, SPECS, tracer).execute(decision())

    assert agents.finished == [INFRA]  # the slower agent was not abandoned mid-call
    entries = orchestration_payload(tracer)["agents"]
    assert entries[0]["error"] == "budget exhausted: max_tool_calls"


async def test_findings_follow_the_selected_order_not_the_finishing_order() -> None:
    agents = FakeAgents({INFRA: sleep_for(0.1), TELEMETRY: sleep_for(0)})
    findings = await Orchestrator(agents, SPECS, Tracer("run-test")).execute(
        decision([INFRA, TELEMETRY])
    )

    assert agents.finished == [TELEMETRY, INFRA]
    assert [finding.agent_role for finding in findings] == [INFRA, TELEMETRY]


async def test_each_agent_gets_its_own_frozen_plan() -> None:
    agents = FakeAgents({})
    focus = ["payment"]
    split = decision(budget={"telemetry_investigator": 3}, round_number=2)
    await Orchestrator(agents, SPECS, Tracer("run-test")).execute(split, focus)

    plans = {plan.role: plan for plan in agents.plans}
    telemetry, infrastructure = plans[TELEMETRY], plans[INFRA]
    assert (telemetry.max_steps, infrastructure.max_steps) == (3, 8)  # missing -> spec.max_steps
    assert telemetry.round == infrastructure.round == 2
    assert telemetry.focus_services == infrastructure.focus_services == ["payment"]
    # Nothing mutable is shared: every plan holds its own copy of the focus list.
    assert telemetry.focus_services is not infrastructure.focus_services
    assert all(plan.focus_services is not focus for plan in agents.plans)
    with pytest.raises(ValidationError):
        telemetry.max_steps = 99  # type: ignore[misc]


async def test_cancellation_propagates() -> None:
    agents = FakeAgents({TELEMETRY: sleep_for(5), INFRA: sleep_for(5)})
    task = asyncio.create_task(Orchestrator(agents, SPECS, Tracer("run-test")).execute(decision()))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert agents.finished == []


async def test_a_role_without_a_spec_is_a_configuration_error() -> None:
    specs = {TELEMETRY: SPECS[TELEMETRY]}
    with pytest.raises(ConfigError, match="infrastructure_investigator"):
        await Orchestrator(FakeAgents({}), specs, Tracer("run-test")).execute(decision())


async def test_the_run_deadline_stops_agents_as_budget_exhausted() -> None:
    """An agent may never outlive the run's own wall-clock budget (audit finding 2.6)."""
    agents = FakeAgents({TELEMETRY: sleep_for(5), INFRA: sleep_for(5)})
    orchestrator = Orchestrator(
        agents,
        SPECS,
        recording_tracer(),
        agent_timeout_seconds=60,  # the agents' own limit is far away...
        remaining_seconds=lambda: 0.1,  # ...but the run has only 0.1 s left
    )
    started = time.perf_counter()

    with pytest.raises(BudgetExhausted) as stopped:
        await orchestrator.execute(decision())

    assert stopped.value.limit == "max_elapsed_seconds"
    assert time.perf_counter() - started < 1.0  # cut at the deadline, not after 5 s


async def test_an_agent_limit_shorter_than_the_run_deadline_is_still_an_agent_error() -> None:
    agents = FakeAgents({INVESTIGATOR: sleep_for(5)})
    orchestrator = Orchestrator(
        agents, SPECS, recording_tracer(), agent_timeout_seconds=0.1, remaining_seconds=lambda: 60
    )

    [finding] = await orchestrator.execute(decision([INVESTIGATOR], StrategyMode.SINGLE))

    assert finding.error == "timeout after 0.1s"
