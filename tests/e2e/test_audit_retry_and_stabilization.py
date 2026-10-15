"""AUDIT-2.6 (recovery and execution lifecycle): what happens after a fix is executed.

IncidentController._resolve runs: investigate -> propose -> approve -> fix -> verify, and
retries with a new investigation round when the oracle says "not recovered". The audit found
weak spots in that loop. In simple words:

1. Stale evidence after a fix that only half worked. After a failed fix a new "epoch"
   starts and the agents look again, but the verifier still judges with ALL evidence of the
   run. Observations from BEFORE the fix (e.g. OOM kills of a service that was already
   restarted) keep supporting the old diagnosis, so the second round blames the service that
   was already fixed and gives up. No test reached RESOLVED on a second fix.
   Reproduced with a two-fault scenario written into tmp_path: payment leaks memory AND
   checkout runs a bad release. Restarting payment fixes only half; a correct controller then
   investigates again and rolls back checkout. Today the run ends FAILED ("no remediation
   could be drafted") because round 2 diagnoses payment again.
   A second problem sits on the same path: once a run holds this much evidence, the bounded
   proposal context pack drops the very runbook chunk that names the fix for the diagnosis
   (context loss, AUDIT-2.1). The scripted policy then falls back to a blind restart, which
   cannot fix a bad release. A separate test pins that part down, so each fix has its own test.

2. No stabilization wait. The oracle is probed once, immediately after the write. The
   simulator moves its clock 5 minutes per action, so recovery there is instant, but a live
   target needs time (pods restart, readiness probes pass). Reproduced with a small
   NON-simulated environment whose health only turns green 1.5 s after the fix: today the
   controller judges too early and calls the right fix "not recovered".

3. Wall-clock limits around provider calls (a slow or hanging LLM). Several slow calls do
   stop the run as BUDGET_EXHAUSTED (guard), but a single call that outlasts the whole budget
   is reported as an agent failure (FAILED) during investigation, and a proposal call has no
   deadline at all, so the run overruns its budget until the call returns.

Tests marked xfail(strict=True) reproduce a defect: they assert the CORRECT behaviour and fail
today; they must pass once M1 fixes the defect (strict, so an unexpected pass is reported too).
Tests without the mark are guards: behaviour that already works and must keep working.

Every run uses the real controller and a real MCP tool server subprocess (stdio).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from recap.core.config import EnvironmentConfig, Settings
from recap.core.models import (
    ApprovalRequest,
    EnvironmentInfo,
    IncidentState,
    InvestigationDecision,
    InvestigationRequest,
    MitigationDraft,
    ProposalDecision,
    ProposalRequest,
    RunBudget,
    RunConfig,
    TraceEvent,
)
from recap.core.observations import ActionOutcome, AuthorizedAction
from recap.environments.registry import create_environment
from recap.providers.deterministic import DeterministicProvider
from recap.runtime import Runtime
from recap.safety.approvals import ApprovalStore
from recap.telemetry.tracer import TraceCategory as C
from tests.fakes import BAD, FakeEnvironment

AUTO_NO_MEMORY = RunConfig(approval_mode="auto", memory_write=False)


# --- helpers ---------------------------------------------------------------------------------


def with_scenarios(settings: Settings, scenarios_dir: Path) -> Settings:
    """Settings whose simulator reads scenario files from `scenarios_dir`."""
    options = dict(settings.environment.options, scenarios_dir=str(scenarios_dir))
    environment = settings.environment.model_copy(update={"options": options})
    return settings.model_copy(update={"environment": environment})


def writes(runtime: Runtime, run_id: str) -> list[TraceEvent]:
    """Every write sent to the target in a run (the gateway traces each write exactly once)."""
    return [e for e in runtime.events.list_events(run_id) if e.category == C.MITIGATION]


# --- 1. a fix that only half works, then a second, separately investigated fix ---------------

TWO_FAULT_ID = "two-fault-retry"


def write_two_fault_scenario(settings: Settings, folder: Path) -> None:
    """Copy the shipped bad-deployment.yaml into `folder` and give it a second fault.

    The changes from bad-deployment.yaml:
      * payment leaks memory (OOM-killed for 8 minutes), which breaks its caller checkout;
      * checkout ALSO runs a bad release v2.5.0 (deployed 20 minutes ago, after v2.4.0),
        so restarting payment alone cannot make checkout healthy again;
      * the alert names checkout and frontend (frontend calls checkout).
    """
    shipped = Path(settings.environment.options["scenarios_dir"]) / "bad-deployment.yaml"
    spec: dict[str, Any] = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    spec["id"] = TWO_FAULT_ID  # a scenario id must equal its file name
    spec["title"] = "Checkout and frontend failing: two faults at once"
    spec["description"] = "Payment leaks memory and checkout runs a bad release (AUDIT-2.6)."
    spec["tags"] = []
    spec["alert"]["affected_services"] = ["checkout", "frontend"]
    spec["alert"]["symptoms"].append("frontend: checkout page error rate above 25%")
    # Checkout's history must end with the version it runs: v2.4.0 two days ago, then v2.5.0.
    spec["services"]["checkout"]["version"] = "v2.5.0"
    spec["deployments"] = [
        d for d in spec["deployments"] if d["service"] != "checkout" or d["version"] == "v2.4.0"
    ]
    spec["deployments"].append(
        {"service": "checkout", "version": "v2.5.0", "minutes_ago": 20, "change": "Cart rewrite"}
    )
    spec["faults"] = [
        {
            "type": "memory_leak",
            "service": "payment",
            "started_minutes_ago": 50,
            "oom_minutes_ago": 8,
            "error_rate": 0.5,
        },
        {
            "type": "bad_release",
            "service": "checkout",
            "version": "v2.5.0",
            "started_minutes_ago": 19,
            "error_rate": 0.25,
            "latency_ms": 1500,
            "exception": "IllegalStateException in CartTotals.compute",
        },
    ]
    # Two faults need two full investigation rounds. The current code used 29 of the default
    # 30 steps before it gave up, so the scenario gives itself headroom. The limits stay finite,
    # so a retry loop would still be stopped. (`expected` is copied unchanged: eval-only data.)
    spec["budget"] = {"max_steps": 60, "max_tool_calls": 60, "max_estimated_tokens": 240_000}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{TWO_FAULT_ID}.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), "utf-8")


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.6: pre-fix evidence re-blames the already fixed service, so no second "
    "fix is tried (the rollback also needs the AUDIT-2.1 runbook packing fix); fixed in M1",
)
async def test_a_half_working_fix_is_followed_by_a_separately_investigated_second_fix(
    settings: Settings, tmp_path: Path
) -> None:
    """Two faults: the first fix helps only half, a new round finds the second; RESOLVED."""
    scenarios = tmp_path / "scenarios"
    write_two_fault_scenario(settings, scenarios)
    runtime = Runtime(with_scenarios(settings, scenarios))

    record = await runtime.run_incident(TWO_FAULT_ID, RunConfig(approval_mode="auto"))

    # Today: FAILED, "no remediation could be drafted", and the last diagnosis is still
    # "resource pressure on payment" although payment was already restarted.
    last = record.verification.summary if record.verification else "none"
    assert record.state == IncidentState.RESOLVED, (
        f"{record.state.value}: {record.outcome_note}; last diagnosis: {last}"
    )
    assert record.recovery is not None and record.recovery.verified

    # Two different fixes, each executed once. The first one only fixed half the system.
    assert [a.result for a in record.actions] == ["not_recovered", "recovered"]
    assert {(a.action, a.target) for a in record.actions} == {
        ("restart_service", "payment"),
        ("execute_rollback", "checkout"),
    }
    sent = writes(runtime, record.run_id)
    assert len(sent) == len(record.actions) <= settings.budget.retries.max_mitigation_attempts

    # Every executed fix had its own exact approval, and each approval was used up (single use).
    approval_ids = [event.payload["approval_id"] for event in sent]
    assert len(set(approval_ids)) == len(sent)
    for approval_id in approval_ids:
        approval = runtime.approvals.get_approval(approval_id)
        assert approval is not None and approval.decision == "approved"
        assert approval.consumed_at is not None

    # The second fix came from a NEW investigation round that looked at the system again:
    # right after the first verification the run went back to INVESTIGATING, and the second
    # proposal cites evidence that was observed after the first fix.
    states = [change.state for change in record.history]
    assert states[states.index(IncidentState.VERIFYING) + 1] == IncidentState.INVESTIGATING
    first_fix_done = record.actions[0].at
    fresh = {e.id for e in runtime.runs.evidence(record.run_id) if e.timestamp > first_fix_done}
    assert set(record.proposals[-1].evidence_ids) & fresh


async def test_the_runbook_for_the_diagnosis_reaches_the_proposal(
    settings: Settings, tmp_path: Path
) -> None:
    """Each fix is drafted from the runbook RAG fetched for its diagnosis, not a fallback."""
    scenarios = tmp_path / "scenarios"
    write_two_fault_scenario(settings, scenarios)
    runtime = Runtime(with_scenarios(settings, scenarios))

    record = await runtime.run_incident(TWO_FAULT_ID, AUTO_NO_MEMORY)

    # The packer lists what it left out of each proposal's context pack (trace "dropped").
    dropped = [
        event.payload["dropped"]
        for event in runtime.events.list_events(record.run_id)
        if event.category == C.CONTEXT and event.payload.get("stage") == "proposal"
    ]
    drafted = [(p.action, p.target, p.source) for p in record.proposals]
    assert record.proposals, f"no fix was drafted: {record.outcome_note}"
    # Today: ["fallback"], because the pack dropped "runbook-memory-pressure#remediation-1",
    # the chunk that RAG retrieved for exactly this diagnosis (resource pressure on payment).
    sources = [p.source for p in record.proposals]
    assert sources == ["runbook"] * len(sources), (
        f"proposals {drafted}; left out of the proposal packs: {dropped}"
    )


# --- 2. a live target needs time to stabilize after a fix -----------------------------------

STABILIZE_SECONDS = 1.5  # how long the live-like target needs after the fix
RUN_LIMIT_SECONDS = 60.0  # outer safety net: a correct wait is bounded well below this
# The MCP tool server runs in a subprocess and builds its own copy of the environment from
# this import path (like "tests.fakes:FakeEnvironment" in tests/e2e/test_environment_swap.py).
LIVE_KIND = "tests.e2e.test_audit_retry_and_stabilization:SlowRolloutEnvironment"


class SlowRolloutEnvironment(FakeEnvironment):
    """The tests' fake shop, presented as a live (NOT simulated) target with a slow rollout.

    The rollback itself is accepted at once, but the new pods need STABILIZE_SECONDS of real
    time before they serve traffic cleanly. Until then every observation and the oracle's
    health check still show the errors, like a Kubernetes rollout that is still in progress.
    """

    kind = "live-like"

    def describe(self) -> EnvironmentInfo:
        """Like the fake, but not simulated, so the auto-approver is not allowed here."""
        info = super().describe()
        return info.model_copy(update={"name": "live-like test target", "is_simulated": False})

    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        """Apply the fix and note when it happened (wall clock: the oracle's process sees it)."""
        outcome = await super().execute_authorized(action)
        if outcome.ok:
            state = self._state()
            state["fixed_at"] = time.time()
            self._save(state)
        return outcome

    def _broken(self) -> bool:
        """Broken while the bad version runs, and for STABILIZE_SECONDS after the fix."""
        state = self._state()
        if state["checkout_version"] == BAD:
            return True
        return time.time() - state.get("fixed_at", 0.0) < STABILIZE_SECONDS


class OperatorApproves:
    """Approval channel standing in for a human who approves each exact request.

    A non-simulated run may not use the auto-approver, so this records the decision through
    the ApprovalStore, which applies the same checks as for a real human.
    """

    name = "test operator"

    def __init__(self, store: ApprovalStore) -> None:
        self.store = store

    async def notify(self, request: ApprovalRequest) -> None:
        """Approve exactly the proposal in the request (its digest must match)."""
        self.store.decide(
            request.proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor="test-operator",
        )


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.6: the oracle is probed once right after a write, with no stabilization "
    "wait for a live target; fixed in M1",
)
async def test_a_live_fix_is_verified_after_the_target_has_stabilized(
    settings: Settings, tmp_path: Path
) -> None:
    """A live target needs 1.5 s to settle: the controller waits (bounded) and resolves."""
    live = settings.model_copy(
        update={
            "environment": EnvironmentConfig(
                kind=LIVE_KIND, options={"state_dir": str(tmp_path / "live")}
            )
        }
    )
    runtime = Runtime(live)
    assert runtime.environment_info().is_simulated is False

    started = time.monotonic()
    record = await asyncio.wait_for(  # a wait that never ends would fail here, not hang
        runtime.run_incident(
            "fake-bad-release",
            RunConfig(memory_write=False),  # manual approvals, answered by OperatorApproves
            approval_channel=OperatorApproves(runtime.approvals),
        ),
        timeout=RUN_LIMIT_SECONDS,
    )
    elapsed = time.monotonic() - started

    # Today: the rollback is judged "not_recovered" a few milliseconds after it was applied.
    assert record.actions, f"no fix was executed: {record.state.value}: {record.outcome_note}"
    first = record.actions[0]
    assert (first.action, first.target) == ("execute_rollback", "checkout")
    assert first.result == "recovered", (
        "the oracle judged the rollback right after the write, before the target stabilized"
    )
    assert record.state == IncidentState.RESOLVED
    assert [a.result for a in record.actions] == ["recovered"]  # no second, useless fix
    assert record.recovery is not None and record.recovery.verified
    assert record.approvals and record.approvals[0].consumed_at is not None
    assert STABILIZE_SECONDS <= elapsed < RUN_LIMIT_SECONDS  # it waited, but not forever


async def test_simulated_recovery_is_still_judged_without_waiting(settings: Settings) -> None:
    """Guard: the simulator recovers instantly, so its runs must not wait to verify."""
    runtime = Runtime(settings)

    record = await runtime.run_incident("bad-deployment", AUTO_NO_MEMORY)

    assert record.state == IncidentState.RESOLVED
    assert [a.result for a in record.actions] == ["recovered"]
    at = {change.state: change.at for change in record.history}
    waited = (at[IncidentState.RESOLVED] - at[IncidentState.VERIFYING]).total_seconds()
    assert waited < 1.0, f"verification of a simulated fix took {waited:.2f}s"


# --- 3. budgets and attempt limits ------------------------------------------------------------


class SlowProposer(DeterministicProvider):
    """The scripted policy, but every proposal takes `delay` seconds (like a slow LLM)."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        await asyncio.sleep(self.delay)
        return await super().propose_mitigation(request)


async def test_a_slow_provider_runs_out_the_wall_clock_and_no_fix_is_sent(
    settings: Settings,
) -> None:
    """Guard: a proposal slower than max_elapsed_seconds ends BUDGET_EXHAUSTED, no write."""
    runtime = Runtime(settings)
    runtime.make_provider = lambda config: SlowProposer(delay=1.5)  # type: ignore[method-assign]
    config = AUTO_NO_MEMORY.model_copy(update={"budget": RunBudget(max_elapsed_seconds=1.0)})

    record = await runtime.run_incident("bad-deployment", config)

    assert record.state == IncidentState.BUDGET_EXHAUSTED
    assert "max_elapsed_seconds" in record.outcome_note
    assert record.error is None
    # Nothing reached the target: no write traced or recorded, nothing in the sandbox's log,
    # and no approval was used up.
    assert writes(runtime, record.run_id) == []
    assert record.actions == [] and record.recovery is None
    assert all(approval.consumed_at is None for approval in record.approvals)
    sandbox = create_environment(runtime.settings.environment, record.run_id)
    assert sandbox.store.load()["action_log"] == []  # type: ignore[attr-defined]


HANG_BUDGET_SECONDS = 2.0  # small, but enough for the fast scripted investigation itself
HANG_SECONDS = 4.5  # how long the hanging provider call would take if nobody stopped it
STOP_SLACK_SECONDS = 1.5  # time allowed after the deadline to stop the tool server and save


class HangingProvider(DeterministicProvider):
    """The scripted policy, but its calls in one stage hang far past the run's budget."""

    def __init__(self, stage: str) -> None:
        super().__init__()
        self.stage = stage  # "investigation" (next_action) or "proposal" (propose_mitigation)

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        if self.stage == "investigation":
            await asyncio.sleep(HANG_SECONDS)
        return await super().next_action(request)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        if self.stage == "proposal":
            await asyncio.sleep(HANG_SECONDS)
        return await super().propose_mitigation(request)


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.6: a provider call that outlasts max_elapsed_seconds is not stopped at "
    "the deadline as BUDGET_EXHAUSTED; fixed in M1",
)
@pytest.mark.parametrize("stage", ["investigation", "proposal"])
async def test_a_provider_call_that_hangs_past_the_wall_clock_budget_is_cut_off(
    settings: Settings, stage: str
) -> None:
    """A hanging provider call is stopped at the deadline and reported as BUDGET_EXHAUSTED."""
    runtime = Runtime(settings)
    runtime.make_provider = lambda config: HangingProvider(stage)  # type: ignore[method-assign]
    budget = RunBudget(max_elapsed_seconds=HANG_BUDGET_SECONDS)
    config = AUTO_NO_MEMORY.model_copy(update={"budget": budget})

    started = time.monotonic()
    record = await runtime.run_incident("bad-deployment", config)
    elapsed = time.monotonic() - started

    # Today, "investigation": FAILED, "every agent in the round failed" (an agent timeout).
    assert record.state == IncidentState.BUDGET_EXHAUSTED, (
        f"{record.state.value}: {record.outcome_note} (error: {record.error})"
    )
    assert "max_elapsed_seconds" in record.outcome_note
    # Today, "proposal": the right state, but only after the full hang (no deadline on it).
    assert elapsed < HANG_BUDGET_SECONDS + STOP_SLACK_SECONDS, (
        f"the run took {elapsed:.1f}s for a {HANG_BUDGET_SECONDS:g}s wall-clock budget"
    )
    assert writes(runtime, record.run_id) == []


async def test_a_tiny_step_budget_stops_the_run_as_budget_exhausted(settings: Settings) -> None:
    """Guard: with max_steps=2 the run stops cleanly as BUDGET_EXHAUSTED before any fix."""
    runtime = Runtime(settings)
    config = AUTO_NO_MEMORY.model_copy(update={"budget": RunBudget(max_steps=2)})

    record = await runtime.run_incident("bad-deployment", config)

    assert record.state == IncidentState.BUDGET_EXHAUSTED
    assert "max_steps" in record.outcome_note
    assert record.usage.steps <= 2
    assert record.actions == [] and record.recovery is None
    assert writes(runtime, record.run_id) == []


class StubbornProvider(DeterministicProvider):
    """Always proposes restarting the diagnosed service, even after that already failed."""

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        diagnosis = request.hypothesis
        draft = MitigationDraft(
            action="restart_service",
            target=diagnosis.target_service or "",
            rationale="restart it (again)",
            evidence_ids=list(diagnosis.supporting_evidence_ids),
            hypothesis_claim=diagnosis.claim,
            source="fallback",
        )
        return ProposalDecision(draft=draft)


@pytest.mark.parametrize("cap", [1, 2])
async def test_executed_fixes_never_exceed_max_mitigation_attempts(
    settings: Settings, cap: int
) -> None:
    """Guard: a provider stuck on a useless fix is stopped after max_mitigation_attempts."""
    # A restart cannot fix a bad release: without the cap this would retry until a budget ran out.
    retries = settings.budget.retries.model_copy(update={"max_mitigation_attempts": cap})
    budget = settings.budget.model_copy(update={"retries": retries})
    runtime = Runtime(settings.model_copy(update={"budget": budget}))
    runtime.make_provider = lambda config: StubbornProvider()  # type: ignore[method-assign]

    record = await runtime.run_incident("bad-deployment", AUTO_NO_MEMORY)

    assert 1 <= len(writes(runtime, record.run_id)) <= cap
    assert record.state == IncidentState.FAILED
    assert record.recovery is not None and not record.recovery.verified
