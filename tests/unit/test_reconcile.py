"""Deciding what an interrupted run did, from its persisted evidence (M6 W3).

The rule under test: a run may run again only when no write can have reached the target. The
write-ahead record (mitigation.started) is written before a write is sent, so without it
nothing was sent; with it, only the target's own audit log can say "not applied", and a target
that cannot tell is "unknown", which is never retried.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from recap.core.ids import utcnow
from recap.core.models import (
    IncidentState,
    Interruption,
    RunConfig,
    RunRecord,
    TraceEvent,
    WriteEvidence,
)
from recap.core.observations import AuthorizedAction
from recap.environments.kubernetes.engine import KubernetesEnvironment
from recap.environments.simulated import SimulatedEnvironment
from recap.environments.simulated.scenario import load_scenario
from recap.harness.store import RunStore
from recap.safety.approvals import ApprovalStore
from recap.telemetry.tracer import TraceCategory as C
from recap.workers.reconcile import assess, run_ended
from tests.helpers import act
from tests.unit.test_approvals import make_proposal
from tests.unit.test_simulator import SCENARIOS


def record(state: IncidentState = IncidentState.INVESTIGATING, note: str = "") -> RunRecord:
    return RunRecord(
        run_id="run-1",
        scenario_id="bad-deployment",
        config=RunConfig(),
        state=state,
        outcome_note=note,
    )


def event(seq: int, category: str, **payload: Any) -> TraceEvent:
    return TraceEvent(
        event_id=f"evt-{seq}",
        run_id="run-1",
        seq=seq,
        stage="X",
        ts=utcnow(),
        category=category,
        payload=payload,
    )


def move(seq: int, frm: str, to: str) -> TraceEvent:
    return event(seq, C.STATE, **{"from": frm, "to": to})


STARTED = event(10, C.MITIGATION_STARTED, invocation_id="inv-1", approval_id="ap-1")
APPLIED = event(11, C.MITIGATION, invocation_id="inv-1", ok=True)
NOT_APPLIED = event(11, C.MITIGATION, invocation_id="inv-1", ok=False, error="refused")


@pytest.mark.parametrize(
    ("the_record", "events", "pending", "evidence", "phase", "write", "again"),
    [
        (None, [], False, None, "before_start", "none", True),
        (
            record(),
            [move(1, "CREATED", "INVESTIGATING")],
            False,
            None,
            "before_write",
            "none",
            True,
        ),
        (
            record(IncidentState.AWAITING_APPROVAL),
            [],
            True,
            None,
            "awaiting_approval",
            "none",
            True,
        ),
        # write-ahead record, no outcome: only the target's audit log decides
        (
            record(IncidentState.MITIGATING),
            [STARTED],
            False,
            None,
            "write_in_flight",
            "unknown",
            False,
        ),
        (
            record(IncidentState.MITIGATING),
            [STARTED],
            False,
            WriteEvidence(state="not_applied"),
            "write_in_flight",
            "not_applied",
            True,
        ),
        (
            record(IncidentState.MITIGATING),
            [STARTED],
            False,
            WriteEvidence(state="refused"),
            "write_in_flight",
            "refused",
            True,
        ),
        (
            record(IncidentState.MITIGATING),
            [STARTED],
            False,
            WriteEvidence(state="applied"),
            "write_in_flight",
            "applied",
            False,
        ),
        (
            record(IncidentState.MITIGATING),
            [STARTED],
            False,
            WriteEvidence(state="unknown"),
            "write_in_flight",
            "unknown",
            False,
        ),
        # the outcome is recorded
        (
            record(IncidentState.MITIGATING),
            [STARTED, APPLIED],
            False,
            None,
            "after_write",
            "applied",
            False,
        ),
        (
            record(IncidentState.VERIFYING),
            [STARTED, APPLIED],
            False,
            None,
            "verifying",
            "applied",
            False,
        ),
        # recorded as not applied, but the target was not asked: still unknown
        (
            record(IncidentState.MITIGATING),
            [STARTED, NOT_APPLIED],
            False,
            None,
            "after_write",
            "unknown",
            False,
        ),
    ],
)
def test_a_run_may_run_again_only_when_no_write_can_have_reached_the_target(
    the_record: RunRecord | None,
    events: list[TraceEvent],
    pending: bool,
    evidence: WriteEvidence | None,
    phase: str,
    write: str,
    again: bool,
) -> None:
    verdict = assess(the_record, events, pending_approval=pending, write_evidence=evidence)
    assert (verdict.phase, verdict.write, verdict.may_run_again) == (phase, write, again)
    assert verdict.started == (the_record is not None or bool(events))


def test_a_run_its_worker_cancelled_is_judged_by_where_it_was_cancelled() -> None:
    cancelled = record(IncidentState.FAILED, "run cancelled")
    waiting = [move(5, "PROPOSING", "AWAITING_APPROVAL"), move(6, "AWAITING_APPROVAL", "FAILED")]
    verdict = assess(cancelled, waiting, pending_approval=False, write_evidence=None)
    assert verdict.phase == "awaiting_approval" and verdict.may_run_again
    verifying = [STARTED, APPLIED, move(12, "VERIFYING", "FAILED")]
    verdict = assess(cancelled, verifying, pending_approval=False, write_evidence=None)
    assert verdict.phase == "verifying" and not verdict.may_run_again


def test_the_trace_is_ahead_of_the_checkpoint_after_a_crash() -> None:
    """The controller traces a move before it saves the checkpoint: killed in between, the
    record still says MITIGATING while the trace already entered VERIFYING."""
    behind = record(IncidentState.MITIGATING)
    events = [STARTED, APPLIED, move(12, "MITIGATING", "VERIFYING")]
    verdict = assess(behind, events, pending_approval=False, write_evidence=None)
    assert (verdict.phase, verdict.write) == ("verifying", "applied")


def test_a_run_ended_only_with_a_final_state_and_its_summary() -> None:
    summary = event(20, C.RUN_SUMMARY)
    assert run_ended(record(IncidentState.RESOLVED), [summary])
    assert not run_ended(record(IncidentState.RESOLVED), [])  # the summary was never written
    assert not run_ended(record(IncidentState.MITIGATING), [summary])
    assert not run_ended(None, [summary])


# --- the targets' own audit logs ----------------------------------------------------------------


async def test_the_simulator_s_action_log_proves_whether_a_write_was_applied(
    tmp_path: Path,
) -> None:
    env = SimulatedEnvironment(
        run_id="run-sim", scenarios_dir=str(SCENARIOS), state_dir=str(tmp_path), write_latency_ms=0
    )
    assert env.recorded_write("ap-1", "digest-1").state == "not_applied"  # never reset
    await env.reset("bad-deployment", load_scenario(SCENARIOS, "bad-deployment").seed)
    assert env.recorded_write("ap-1", "digest-1").state == "not_applied"
    unknown = AuthorizedAction(
        action="restart", target="no-such-service", approval_id="ap-2", proposal_digest="digest-2"
    )
    assert not (await act(env, unknown)).ok  # authorized, but the simulator rejected it
    assert env.recorded_write("ap-2", "digest-2").state == "refused"
    fix = AuthorizedAction(
        action="restart", target="checkout", approval_id="ap-1", proposal_digest="digest-1"
    )
    assert (await act(env, fix)).ok
    assert env.recorded_write("ap-1", "digest-1").state == "applied"
    assert env.recorded_write("ap-1", "another-digest").state == "not_applied"


def test_a_cluster_without_an_audit_row_is_unknown_never_unchanged() -> None:
    def judged(rows: list[dict[str, Any]]) -> str:
        env = object.__new__(KubernetesEnvironment)
        env.state = SimpleNamespace(writes=lambda: rows)  # type: ignore[attr-defined]
        return env.recorded_write("ap-1", "d-1").state

    row = {"approval_id": "ap-1", "proposal_digest": "d-1"}
    assert judged([]) == "unknown"  # audited only after the cluster answers: proves nothing
    assert judged([{**row, "outcome": "executed"}]) == "applied"
    assert judged([{**row, "outcome": "refused"}]) == "refused"
    assert judged([{**row, "outcome": "failed"}]) == "unknown"
    assert judged([{**row, "approval_id": "ap-other", "outcome": "executed"}]) == "unknown"


# --- what a closed run keeps ----------------------------------------------------------------------


def test_a_run_closed_after_its_worker_stopped_is_final(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "recap.db")
    closed = record(IncidentState.FAILED, "interrupted (before write)")
    closed.interruption = Interruption(
        at=utcnow(), phase="before_write", write="none", needs_reconciliation=False, detail="x"
    )
    store.save(closed)
    late = record(IncidentState.MITIGATING)  # a frozen worker woke up and saved its checkpoint
    store.save(late)
    kept = store.get("run-1")
    assert kept is not None and kept.state == IncidentState.FAILED and kept.interruption is not None
    closed.outcome_note = "updated by a later settling"
    store.save(closed)  # a record that carries the interruption still updates
    again = store.get("run-1")
    assert again is not None and again.outcome_note == "updated by a later settling"


def test_voiding_a_stopped_run_s_approvals_keeps_a_used_one(tmp_path: Path) -> None:
    approvals = ApprovalStore(tmp_path / "recap.db")
    pending, approved, used, other = (make_proposal(f"prop-{n}") for n in range(4))
    for proposal, run_id in (
        (pending, "run-1"),
        (approved, "run-1"),
        (used, "run-1"),
        (other, "run-2"),
    ):
        approvals.create_request(proposal, run_id, ttl_seconds=600)
    decisions = [
        approvals.decide(
            proposal.proposal_id, decision="approved", proposal_digest=proposal.digest(), actor="op"
        )
        for proposal in (approved, used)
    ]
    approvals.consume(decisions[1].approval_id)  # the gateway used it before a write
    changed = approvals.void_for_run("run-1", "the worker stopped")
    assert sorted(changed) == ["prop-0: expired", "prop-1: revoked"]  # the used one stays
    left = approvals.get_request(other.proposal_id)
    assert left is not None and left.status.value == "pending"  # another run's request
    assert approvals.void_for_run("run-1", "again") == []  # nothing left to void
