"""Tests for the shared contracts: lifecycle rules, budgets, evidence IDs, digests, tracer."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from recap.context.evidence import EvidenceRegistry, tool_source_ref
from recap.core.errors import BudgetExhausted, IllegalTransition
from recap.core.ids import utcnow
from recap.core.lifecycle import Lifecycle, allowed_next
from recap.core.models import (
    TERMINAL_STATES,
    IncidentState,
    MitigationProposal,
    RunBudget,
    RunConfig,
    RunRecord,
    SourceType,
    Usage,
)
from recap.core.observations import AlertSignal
from recap.core.signals import minutes_between
from recap.harness.budget import BudgetTracker
from recap.telemetry.tracer import MemorySink, Tracer

S = IncidentState


# --- lifecycle ------------------------------------------------------------------------------


def test_happy_path_transitions_are_legal() -> None:
    lifecycle = Lifecycle()
    for state in (
        S.RETRIEVING,
        S.INVESTIGATING,
        S.PROPOSING,
        S.AWAITING_APPROVAL,
        S.MITIGATING,
        S.VERIFYING,
        S.RESOLVED,
    ):
        lifecycle.transition(state)
    assert lifecycle.is_terminal
    assert [c.state for c in lifecycle.history][0] == S.CREATED


def test_cannot_skip_to_resolved_without_verification() -> None:
    lifecycle = Lifecycle()
    lifecycle.transition(S.RETRIEVING)
    lifecycle.transition(S.INVESTIGATING)
    with pytest.raises(IllegalTransition):
        lifecycle.transition(S.RESOLVED)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert allowed_next(state) == set()


def test_any_active_state_can_fail_or_exhaust_budget() -> None:
    for state in set(S) - TERMINAL_STATES:
        assert {S.FAILED, S.BUDGET_EXHAUSTED} <= allowed_next(state)


def test_failed_mitigation_may_reinvestigate() -> None:
    assert S.INVESTIGATING in allowed_next(S.VERIFYING)


# --- budget ----------------------------------------------------------------------------------


def test_tool_call_reservation_stops_before_exceeding() -> None:
    budget = BudgetTracker(RunBudget(max_tool_calls=2))
    budget.reserve_tool_call()
    budget.reserve_tool_call()
    with pytest.raises(BudgetExhausted) as info:
        budget.reserve_tool_call()
    assert info.value.limit == "max_tool_calls"
    assert budget.tool_calls == 2  # the refused call was never counted as made


def test_steps_and_tokens_are_bounded() -> None:
    budget = BudgetTracker(RunBudget(max_steps=1, max_estimated_tokens=10))
    budget.charge_step()
    with pytest.raises(BudgetExhausted):
        budget.charge_step()
    budget.charge_tokens(10)
    with pytest.raises(BudgetExhausted):
        budget.charge_tokens(1)


def test_paused_time_is_not_counted() -> None:
    now = [0.0]
    budget = BudgetTracker(RunBudget(max_elapsed_seconds=5), clock=lambda: now[0])
    now[0] = 2.0
    budget.pause_clock()
    now[0] = 100.0  # a long human approval wait
    budget.resume_clock()
    assert budget.elapsed_seconds() == pytest.approx(2.0)
    budget.check_deadline()  # still within budget
    now[0] = 104.0
    with pytest.raises(BudgetExhausted):
        budget.check_deadline()


# --- evidence ---------------------------------------------------------------------------------


def test_evidence_ids_are_run_unique_and_records_are_immutable() -> None:
    registry = EvidenceRegistry("run-abc123def0", "inc-1")
    first = registry.add(source_type=SourceType.TOOL, source_ref="x", content="hello")
    second = registry.add(source_type=SourceType.TOOL, source_ref="y", content="world")
    assert first.id == "ev-abc123-001" and second.id == "ev-abc123-002"
    assert registry.exists(first.id) and not registry.exists("ev-other-001")
    with pytest.raises(ValidationError):
        first.content = "tampered"  # frozen model: evidence cannot be edited


def test_tool_source_ref_is_canonical() -> None:
    a = tool_source_ref("get_service_logs", {"service": "checkout", "limit": 10})
    b = tool_source_ref("get_service_logs", {"limit": 10, "service": "checkout"})
    assert a == b == "mcp:get_service_logs(limit=10,service=checkout)"


# --- contracts ---------------------------------------------------------------------------------


def _proposal(**overrides: object) -> MitigationProposal:
    data = dict(
        proposal_id="prop-1",
        incident_id="inc-1",
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="test",
    )
    data.update(overrides)
    return MitigationProposal(**data)  # type: ignore[arg-type]


def test_digest_binds_the_exact_action() -> None:
    base = _proposal()
    assert base.digest() == _proposal(rationale="different words").digest()
    assert base.digest() != _proposal(params={"to_version": "v1.0.0"}).digest()
    assert base.digest() != _proposal(target="payment").digest()
    assert base.digest() != _proposal(incident_id="inc-2").digest()


def test_run_record_json_round_trip() -> None:
    record = RunRecord(run_id="run-1", scenario_id="bad-deployment", config=RunConfig())
    again = RunRecord.model_validate_json(record.model_dump_json())
    assert again == record


def test_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RunConfig(strategy="single", surprise=True)  # type: ignore[call-arg]


def test_usage_adds_optional_real_token_counts() -> None:
    total = Usage(steps=1, estimated_tokens=10).plus(
        Usage(steps=2, actual_input_tokens=5, is_estimate=False)
    )
    assert (total.steps, total.estimated_tokens, total.actual_input_tokens) == (3, 10, 5)
    assert total.is_estimate is False


# --- tracer ------------------------------------------------------------------------------------


def test_tracer_numbers_and_redacts_events() -> None:
    sink = MemorySink()
    tracer = Tracer("run-1", [sink], redact=lambda p: {k: "***" for k in p})
    tracer.emit("tool.call", {"secret": "value"})
    tracer.stage = "INVESTIGATING"
    tracer.emit("tool.result", {"tool": "x"}, duration_ms=12.5)
    assert [e.seq for e in sink.events] == [1, 2]
    assert sink.events[0].payload == {"secret": "***"}
    assert sink.events[1].stage == "INVESTIGATING" and sink.events[1].duration_ms == 12.5


def test_broken_sink_does_not_break_the_run() -> None:
    class Broken:
        def write(self, event: object) -> None:
            raise RuntimeError("disk full")

    healthy = MemorySink()
    tracer = Tracer("run-1", [Broken(), healthy])
    tracer.emit("x")  # must not raise
    assert len(healthy.events) == 1  # the other sinks still receive the event


def test_recap_timestamps_are_timezone_aware_utc() -> None:
    assert utcnow().tzinfo is not None
    # An adapter may return a naive timestamp; the observation contract stores it as UTC.
    alert = AlertSignal(
        service="checkout", symptoms=["x"], namespace="shop", fired_at=datetime(2026, 1, 1, 12)
    )
    assert alert.fired_at.tzinfo is not None and alert.fired_at.utcoffset() == timedelta(0)
    assert minutes_between(datetime(2026, 1, 1, 12), alert.fired_at) == 0  # naive vs aware is safe


def test_remaining_seconds_counts_down_and_never_goes_negative() -> None:
    now = [100.0]
    budget = BudgetTracker(RunBudget(max_elapsed_seconds=5), clock=lambda: now[0])
    now[0] = 102.0
    assert budget.remaining_seconds() == 3.0
    budget.pause_clock()  # waiting for a human does not use the run's time
    now[0] = 200.0
    assert budget.remaining_seconds() == 3.0
    budget.resume_clock()
    now[0] = 210.0
    assert budget.remaining_seconds() == 0.0
