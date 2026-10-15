"""Orchestrator: runs the agents the router picked, truly at the same time when asked.

Where it sits:

    StrategyDecision --> [Orchestrator.execute] --> one AgentFinding per agent --> verifier

    single    the investigator is awaited directly
    parallel  the specialists run concurrently with asyncio.gather

Why concurrency is safe here: agents share nothing mutable. Each gets its own frozen
AgentRunPlan; the only shared things are the append-only evidence registry (agents exchange
evidence by immutable ID) and the run budget, which counts every step and tool call centrally.

Failure handling, so one broken agent cannot sink the whole run:

    timeout            -> an AgentFinding with error "timeout after Xs" (the others keep theirs)
    crash (exception)  -> an AgentFinding with error "agent crashed: <type>: <message>"
    BudgetExhausted    -> re-raised once every agent has stopped (the budget is shared, so the
                          controller ends the run as BUDGET_EXHAUSTED)
    cancellation       -> propagates (someone is stopping the run)

Each round emits one ORCHESTRATION trace event with every agent's start and finish time.
Its overlap_ms is the proof of concurrency: it is above 0 only if agents ran side by side.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from recap.core.config import AgentSpec
from recap.core.errors import BudgetExhausted, ConfigError
from recap.core.ids import utcnow
from recap.core.models import AgentFinding, AgentRole, Contract, StrategyDecision, StrategyMode
from recap.telemetry.tracer import TraceCategory, Tracer


class AgentRunPlan(Contract):
    """One agent's assignment for one round. Frozen, so agents never share mutable state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AgentRole
    round: int
    max_steps: int
    # Services to examine first: named by the verifier or by an earlier fix attempt.
    focus_services: list[str] = Field(default_factory=list)


# Anything that runs one agent: AgentHarness.run_agent in a real run, a fake in tests.
AgentRunner = Callable[[AgentSpec, AgentRunPlan], Awaitable[AgentFinding]]


@dataclass(frozen=True)
class _Run:
    """How one agent's turn ended (its finding or the exception it raised) and when it ran."""

    plan: AgentRunPlan
    outcome: AgentFinding | Exception
    started_at: datetime  # wall clock, for the dashboard timeline
    finished_at: datetime
    started: float  # monotonic clock, for precise durations
    finished: float

    @property
    def duration_ms(self) -> float:
        return (self.finished - self.started) * 1000


class Orchestrator:
    """Runs the agents of a StrategyDecision and collects their findings."""

    def __init__(
        self,
        runner: AgentRunner,
        specs: Mapping[AgentRole, AgentSpec],
        tracer: Tracer,
        *,
        agent_timeout_seconds: float = 60.0,
    ) -> None:
        self._runner = runner
        self._specs = dict(specs)
        self._tracer = tracer
        self._timeout = agent_timeout_seconds

    async def execute(
        self, decision: StrategyDecision, focus_services: Sequence[str] = ()
    ) -> list[AgentFinding]:
        """Run the selected agents; findings come back in `decision.selected_agents` order."""
        jobs = [self._job(role, decision, focus_services) for role in decision.selected_agents]
        if decision.mode == StrategyMode.PARALLEL:
            runs = await self._run_together(jobs)
        else:
            runs = [await self._run(spec, plan) for spec, plan in jobs]
        self._trace(decision, runs)
        for run in runs:
            if isinstance(run.outcome, BudgetExhausted):
                raise run.outcome  # every agent has stopped; the controller ends the run cleanly
        return [self._finding(run) for run in runs]

    def _job(
        self, role: AgentRole, decision: StrategyDecision, focus_services: Sequence[str]
    ) -> tuple[AgentSpec, AgentRunPlan]:
        """The role's spec plus its own plan (its own copy of the focus list)."""
        spec = self._specs.get(role)
        if spec is None:
            raise ConfigError(f"no agent spec for role '{role.value}' (see configs/agents.yaml)")
        plan = AgentRunPlan(
            role=role,
            round=decision.round,
            max_steps=decision.budget.get(role.value, spec.max_steps),
            focus_services=list(focus_services),
        )
        return spec, plan

    async def _run_together(self, jobs: list[tuple[AgentSpec, AgentRunPlan]]) -> list[_Run]:
        """Start every agent at once and wait until all of them have stopped."""
        results = await asyncio.gather(
            *(self._run(spec, plan) for spec, plan in jobs), return_exceptions=True
        )
        runs: list[_Run] = []
        for result in results:
            if isinstance(result, BaseException):
                raise result  # only cancellation gets here: _run captures every other error
            runs.append(result)
        return runs

    async def _run(self, spec: AgentSpec, plan: AgentRunPlan) -> _Run:
        """Run one agent under its timeout. Errors are captured as the outcome, not raised."""
        started_at, started = utcnow(), time.perf_counter()
        outcome: AgentFinding | Exception
        try:
            outcome = await asyncio.wait_for(self._runner(spec, plan), self._timeout)
        except Exception as exc:  # CancelledError is not an Exception, so cancelling still works
            outcome = exc
        return _Run(plan, outcome, started_at, utcnow(), started, time.perf_counter())

    def _finding(self, run: _Run) -> AgentFinding:
        """The agent's own finding, or an error finding that says why it has none."""
        if isinstance(run.outcome, AgentFinding):
            return run.outcome
        return AgentFinding(
            agent_role=run.plan.role,
            error=self._error_text(run.outcome),
            started_at=run.started_at,
            finished_at=run.finished_at,
            round=run.plan.round,
        )

    def _error_text(self, error: Exception) -> str:
        if isinstance(error, TimeoutError):
            return f"timeout after {self._timeout:g}s"
        if isinstance(error, BudgetExhausted):
            return str(error)  # not a crash: the shared budget ran out
        return f"agent crashed: {type(error).__name__}: {error}"

    def _trace(self, decision: StrategyDecision, runs: list[_Run]) -> None:
        """Emit the round's timeline. overlap_ms > 0 proves the agents ran at the same time."""
        sum_ms = sum(run.duration_ms for run in runs)
        wall_ms = (
            (max(r.finished for r in runs) - min(r.started for r in runs)) * 1000 if runs else 0
        )
        payload: dict[str, Any] = {
            "mode": decision.mode.value,
            "round": decision.round,
            "agents": [self._timeline_entry(run) for run in runs],
            "wall_ms": round(wall_ms, 2),
            "sum_ms": round(sum_ms, 2),
            # Agents that run one after another add up to at most the wall time, so any time
            # beyond it can only come from two agents running at once.
            "overlap_ms": round(max(0.0, sum_ms - wall_ms), 2),
        }
        self._tracer.emit(TraceCategory.ORCHESTRATION, payload, duration_ms=round(wall_ms, 2))

    def _timeline_entry(self, run: _Run) -> dict[str, Any]:
        outcome = run.outcome
        error = outcome.error if isinstance(outcome, AgentFinding) else self._error_text(outcome)
        return {
            "role": run.plan.role.value,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat(),
            "duration_ms": round(run.duration_ms, 2),
            "error": error,
        }
