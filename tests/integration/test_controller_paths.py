"""Controller paths beyond the happy demos: refusals, budget stops, retries, errors, cancellation.

Each test runs the real controller against the simulator through a real MCP subprocess.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from recap.core.config import Settings
from recap.core.errors import RecapError
from recap.core.models import (
    ApprovalStatus,
    IncidentState,
    InvestigationDecision,
    InvestigationRequest,
    MemoryPartition,
    MitigationProposal,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    RunBudget,
    RunConfig,
    ScenarioInfo,
    Usage,
)
from recap.environments.registry import create_environment
from recap.harness.controller import IncidentController
from recap.providers.deterministic import DeterministicProvider
from recap.runtime import Runtime
from recap.safety.approvals import ConsoleApprovalChannel, ExternalApprovalChannel
from recap.telemetry.tracer import TraceCategory as C

AUTO_NO_MEMORY = RunConfig(approval_mode="auto", memory_write=False)
FAKE_KEY = "sk-ant-" + "api03-ABCDEFGHIJKLMNOPQRSTUV"  # assembled: no scanner mistakes it for a key


def categories(runtime: Runtime, run_id: str) -> list[str]:
    return [e.category for e in runtime.events.list_events(run_id)]


def with_scenarios(settings: Settings, scenarios_dir: Path) -> Settings:
    options = dict(settings.environment.options, scenarios_dir=str(scenarios_dir))
    environment = settings.environment.model_copy(update={"options": options})
    return settings.model_copy(update={"environment": environment})


@pytest.mark.e2e
async def test_a_namespace_outside_the_allowlist_is_refused_before_any_tool_call(
    settings: Settings, tmp_path: Path
) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    source = Path(settings.environment.options["scenarios_dir"]) / "bad-deployment.yaml"
    text = source.read_text(encoding="utf-8").replace("namespace: shop", "namespace: prod")
    (scenarios / "bad-deployment.yaml").write_text(text, encoding="utf-8")
    runtime = Runtime(with_scenarios(settings, scenarios))

    record = await runtime.run_incident("bad-deployment", AUTO_NO_MEMORY)

    assert record.state == IncidentState.BLOCKED
    assert "not in the safety policy's allowlist" in record.outcome_note
    assert record.environment and record.environment.namespace == "prod"
    assert record.usage.tool_calls == 0 and C.TOOL_CALL not in categories(runtime, record.run_id)


def test_scenario_overrides_cannot_switch_off_safety_or_approvals(settings: Settings) -> None:
    """A scenario is data: only planner and strategy may come from it."""
    runtime = Runtime(settings)
    controller = IncidentController(
        runtime.controller_deps(), scenario_id="x", config=RunConfig(), run_id="run-cfg0000001"
    )
    controller.scenario = ScenarioInfo(
        id="x",
        title="x",
        family="bad_deployment",
        partition=MemoryPartition.DEMO,
        alert_service="checkout",
        run_overrides={
            "safety_enabled": False,
            "approval_mode": "auto",
            "provider": "anthropic",
            "memory_write_partition": "train",
            "planner": "prompt_injected",
        },
    )
    effective = controller._effective_config()
    assert effective.safety_enabled is True
    assert effective.approval_mode == "manual"
    assert effective.provider is None
    assert effective.memory_write_partition == MemoryPartition.DEMO
    assert effective.planner == "prompt_injected"  # allowed: a red-team scenario may set it


def test_a_fix_marks_exactly_the_services_it_changed(settings: Settings) -> None:
    """After a fix, the verifier ignores the pre-fix state of the services it changed. A
    service-level fix changed that service; a namespace-wide action (possible only in the
    simulation-only safety-off ablation) changed every service, and a namespace is not one."""
    runtime = Runtime(settings)
    controller = IncidentController(
        runtime.controller_deps(), scenario_id="x", config=RunConfig(), run_id="run-fix0000001"
    )

    def changed(action: str, target: str) -> list[str]:
        fix = MitigationProposal(
            proposal_id="prop-1", incident_id="inc-1", action=action, target=target, rationale="t"
        )
        return controller._fixed_services(fix)

    assert changed("restart_service", "payment") == ["payment"]
    assert changed("execute_rollback", "checkout") == ["checkout"]
    every_service = list(settings.policies.allowed_services)
    assert len(every_service) > 1 and "shop" not in every_service
    assert changed("delete_namespace", "shop") == every_service


@pytest.mark.e2e
async def test_running_out_of_token_budget_stops_with_budget_exhausted(settings: Settings) -> None:
    runtime = Runtime(settings)
    config = RunConfig(
        approval_mode="auto", memory_write=False, budget=RunBudget(max_estimated_tokens=500)
    )
    record = await runtime.run_incident("bad-deployment", config)

    assert record.state == IncidentState.BUDGET_EXHAUSTED
    assert "max_estimated_tokens" in record.outcome_note
    assert record.usage.estimated_tokens <= 500
    assert record.recovery is None


@pytest.mark.e2e
async def test_a_fix_that_does_not_recover_is_retried_then_fails_without_repeating(
    settings: Settings,
) -> None:
    """Without runbooks or memory the scripted policy can only restart, which cannot fix a
    bad release: the oracle says 'not recovered', the controller re-investigates, and with
    nothing untried left it fails instead of repeating the same action."""
    runtime = Runtime(settings)
    config = RunConfig(
        strategy="single", use_rag=False, use_memory=False, approval_mode="auto", memory_write=False
    )
    record = await runtime.run_incident("test-inventory-bad-release", config)

    states = [change.state for change in record.history]
    i = states.index(IncidentState.VERIFYING)
    assert states[i + 1] == IncidentState.INVESTIGATING  # the bounded retry loop
    assert [p.action for p in record.proposals] == ["restart_service"]
    assert [a.result for a in record.actions] == ["not_recovered"]
    assert record.state == IncidentState.FAILED
    assert record.recovery and not record.recovery.verified


class BrokenProvider:
    """A provider whose every call fails, like an LLM API that is down."""

    info = ProviderInfo(name="broken", kind="llm", origin="paid_llm", model="none")

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        raise RecapError(f"provider unavailable: HTTP 503 (key {FAKE_KEY})")

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        raise RecapError("provider unavailable: HTTP 503")


@pytest.mark.e2e
async def test_provider_failure_ends_failed_with_the_error_and_writes_no_memory(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    runtime.make_provider = lambda config: BrokenProvider()  # type: ignore[method-assign]
    record = await runtime.run_incident("bad-deployment", RunConfig(approval_mode="auto"))

    assert record.state == IncidentState.FAILED
    assert record.error and "provider unavailable" in record.error
    assert FAKE_KEY not in record.error  # error text is redacted before it is stored
    assert C.RUN_ERROR in categories(runtime, record.run_id)
    assert record.episode_id is None and runtime.memory.count() == 0
    assert len(record.strategies) == 1  # no pointless escalation after every agent failed


async def wait_for_pending(runtime: Runtime, task: asyncio.Task) -> str:  # type: ignore[type-arg]
    for _ in range(400):
        pending = runtime.approvals.pending()
        if pending:
            return pending[0].proposal_id
        assert not task.done(), "run ended before asking for approval"
        await asyncio.sleep(0.05)
    raise AssertionError("no approval request appeared")


@pytest.mark.e2e
async def test_cancelling_a_waiting_run_fails_it_expires_the_request_and_learns_nothing(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    task = asyncio.create_task(
        runtime.run_incident(
            "bad-deployment",
            RunConfig(approval_mode="manual"),
            approval_channel=ExternalApprovalChannel(),
            run_id="run-cancel0001",
        )
    )
    proposal_id = await wait_for_pending(runtime, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = runtime.runs.get("run-cancel0001")
    assert (
        record and record.state == IncidentState.FAILED and record.outcome_note == "run cancelled"
    )
    assert record.episode_id is None and runtime.memory.count() == 0
    request = runtime.approvals.get_request(proposal_id)
    assert request and request.status == ApprovalStatus.EXPIRED  # can never be approved later


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


@pytest.mark.e2e
async def test_cancelling_while_a_fix_is_being_sent_still_records_that_fix(
    settings: Settings,
) -> None:
    """A write that reached the target cannot be called back. The run waits for its outcome,
    records the fix (unverified) with its used approval, and only then stops as cancelled."""
    options = dict(settings.environment.options, write_latency_ms=1500)  # a slow write
    environment = settings.environment.model_copy(update={"options": options})
    runtime = Runtime(settings.model_copy(update={"environment": environment}))
    run_id = "run-cancel0002"
    task = asyncio.create_task(
        runtime.run_incident(
            "bad-deployment", RunConfig(approval_mode="auto", memory_write=False), run_id=run_id
        )
    )
    await wait_for_event(runtime, task, run_id, C.MITIGATION_STARTED)
    task.cancel()  # the write is now in flight in the MCP server
    with pytest.raises(asyncio.CancelledError):
        await task

    record = runtime.runs.get(run_id)
    assert record and record.state == IncidentState.FAILED
    assert record.outcome_note == "run cancelled"
    [fix] = record.actions
    assert (fix.action, fix.target, fix.result) == ("execute_rollback", "checkout", "error")
    assert "cancelled while this fix was being sent" in fix.note
    assert "the tool reported it done" in fix.note and "recovery not verified" in fix.note
    assert record.approvals[-1].consumed_at is not None  # used once, before the write
    [sent] = [e for e in runtime.events.list_events(run_id) if e.category == C.MITIGATION]
    assert sent.payload["ok"] is True and sent.payload["run_cancelled"] is True
    sandbox = create_environment(runtime.settings.environment, run_id)
    assert len(sandbox.store.load()["action_log"]) == 1  # type: ignore[attr-defined]


@pytest.mark.e2e
async def test_a_console_answer_after_the_expiry_blocks_instead_of_crashing(
    settings: Settings,
) -> None:
    budget = settings.budget.model_copy(
        update={"approvals": settings.budget.approvals.model_copy(update={"ttl_seconds": 1})}
    )
    runtime = Runtime(settings.model_copy(update={"budget": budget}))

    def slow_yes(prompt: str) -> str:
        time.sleep(1.5)  # the human answers after the request expired
        return "y"

    channel = ConsoleApprovalChannel(
        runtime.approvals, input_fn=slow_yes, output_fn=lambda *a: None
    )
    record = await runtime.run_incident(
        "bad-deployment",
        RunConfig(approval_mode="manual", memory_write=False),
        approval_channel=channel,
    )
    assert record.state == IncidentState.BLOCKED
    assert record.outcome_note == "approval request expired"
    assert record.error is None and record.recovery is None


@pytest.mark.e2e
async def test_the_trace_says_how_a_console_approval_was_given(settings: Settings) -> None:
    runtime = Runtime(settings)
    channel = ConsoleApprovalChannel(
        runtime.approvals, input_fn=lambda prompt: "y", output_fn=lambda *a: None
    )
    record = await runtime.run_incident(
        "bad-deployment",
        RunConfig(approval_mode="manual", memory_write=False),
        approval_channel=channel,
    )
    assert record.state == IncidentState.RESOLVED
    [decided] = [
        e for e in runtime.events.list_events(record.run_id) if e.category == "approval.decided"
    ]
    assert decided.payload["actor"] == "console-user"
    assert re.fullmatch(
        r"answered after \d+\.\d s \(faster than a person can read the proposal\)",
        decided.payload["note"],
    )


@pytest.fixture(autouse=True)
def _keep_fixtures_untouched(settings: Settings) -> Iterator[None]:
    """Guard: these tests must never modify the shipped scenario files."""
    shipped = Path(settings.environment.options["scenarios_dir"])
    before = {p.name: p.read_bytes() for p in shipped.glob("*.yaml")}
    yield
    assert {p.name: p.read_bytes() for p in shipped.glob("*.yaml")} == before


class PartlyUnreportedProvider(DeterministicProvider):
    """An 'LLM' whose first decision came back without usage data (but was billed)."""

    info = ProviderInfo(name="partly-unreported", kind="llm", origin="paid_llm", model="fake")

    def __init__(self) -> None:
        super().__init__()
        self.first = True

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        decision = await super().next_action(request)
        if self.first:
            self.first = False
            missing = Usage(steps=1, is_estimate=True, unreported_calls=1)
            return decision.model_copy(update={"usage": missing})
        reported = Usage(steps=1, actual_input_tokens=10, actual_output_tokens=5, is_estimate=False)
        return decision.model_copy(update={"usage": reported})


@pytest.mark.e2e
async def test_a_run_with_unreported_llm_usage_never_claims_a_fully_reported_cost(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    runtime.make_provider = lambda config: PartlyUnreportedProvider()  # type: ignore[method-assign]
    record = await runtime.run_incident("bad-deployment", AUTO_NO_MEMORY)

    assert record.usage.unreported_calls == 1
    summary = next(
        e for e in runtime.events.list_events(record.run_id) if e.category == C.RUN_SUMMARY
    )
    assert "not fully reported" in summary.payload["cost"]
