"""Tests for exact-scope approvals: lifecycle, binding, expiry, single use and channels."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import pytest

from recap.core.errors import ApprovalError, ConfigError
from recap.core.ids import utcnow
from recap.core.models import ApprovalStatus, EnvironmentInfo, MitigationProposal
from recap.safety.approvals import (
    ApprovalStore,
    AutoApprovalChannel,
    ConsoleApprovalChannel,
    ExternalApprovalChannel,
)

SIM = EnvironmentInfo(kind="simulated", name="test simulator", namespace="shop", is_simulated=True)


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore()  # in-memory SQLite


def make_proposal(proposal_id: str = "prop-1", **extra: object) -> MitigationProposal:
    fields: dict[str, object] = {
        "proposal_id": proposal_id,
        "incident_id": "inc-1",
        "action": "execute_rollback",
        "target": "checkout",
        "params": {"to_version": "v2.4.0"},
        "rationale": "errors started right after v2.4.1",
        "evidence_ids": ["ev-1-001"],
        **extra,
    }
    return MitigationProposal(**fields)


def approve(store: ApprovalStore, proposal: MitigationProposal, **kwargs: object):
    return store.decide(
        proposal.proposal_id,
        decision="approved",
        proposal_digest=proposal.digest(),
        actor="alice",
        **kwargs,
    )


# --- lifecycle ---------------------------------------------------------------------------


def test_create_decide_consume_lifecycle(store: ApprovalStore) -> None:
    proposal = make_proposal()
    request = store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    assert request.status == ApprovalStatus.PENDING
    assert request.proposal_digest == proposal.digest()
    assert [r.proposal_id for r in store.pending()] == ["prop-1"]

    approval = approve(store, proposal)
    assert approval.decision == "approved"
    assert approval.scope == "single_action"
    assert approval.proposal_digest == proposal.digest()
    assert approval.expires_at == request.expires_at
    assert approval.consumed_at is None
    assert store.get_request("prop-1").status == ApprovalStatus.APPROVED
    assert store.pending() == []

    used = store.consume(approval.approval_id)
    assert used.consumed_at is not None
    assert store.get_approval(approval.approval_id).consumed_at is not None
    assert [a.approval_id for a in store.approvals_for("inc-1")] == [approval.approval_id]
    assert store.approvals_for("inc-other") == []


def test_request_round_trips_the_full_proposal(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    stored = store.get_request("prop-1")
    assert stored.proposal == proposal
    assert stored.run_id == "run-1"


def test_duplicate_request_and_unknown_proposal_raise(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    with pytest.raises(ApprovalError, match="exists"):
        store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    with pytest.raises(ApprovalError, match="unknown proposal"):
        approve(store, make_proposal("prop-unknown"))
    assert store.get_request("prop-unknown") is None
    assert store.get_approval("apr-unknown") is None


def test_decision_with_a_different_digest_is_rejected(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    edited = proposal.model_copy(update={"params": {"to_version": "v1.0.0"}})
    with pytest.raises(ApprovalError, match="different proposal"):
        approve(store, edited)
    assert store.get_request("prop-1").status == ApprovalStatus.PENDING


def test_a_request_can_be_decided_only_once(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.decide("prop-1", decision="rejected", proposal_digest=proposal.digest(), actor="bob")
    with pytest.raises(ApprovalError, match="not pending"):
        approve(store, proposal)
    assert store.get_request("prop-1").status == ApprovalStatus.REJECTED


def test_deciding_after_expiry_raises_and_marks_expired(store: ApprovalStore) -> None:
    proposal = make_proposal()
    request = store.create_request(proposal, run_id="run-1", ttl_seconds=60)
    with pytest.raises(ApprovalError, match="expired"):
        approve(store, proposal, now=request.requested_at + timedelta(seconds=61))
    assert store.get_request("prop-1").status == ApprovalStatus.EXPIRED


def test_stale_requests_are_marked_expired_when_read(store: ApprovalStore) -> None:
    store.create_request(
        make_proposal(), run_id="run-1", ttl_seconds=60, now=utcnow() - timedelta(hours=1)
    )
    assert store.get_request("prop-1").status == ApprovalStatus.EXPIRED
    assert store.pending() == []


def test_approvals_are_single_use(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)
    store.consume(approval.approval_id)
    with pytest.raises(ApprovalError, match="already used"):
        store.consume(approval.approval_id)


def test_rejected_unknown_or_expired_approvals_cannot_be_consumed(store: ApprovalStore) -> None:
    rejected, late = make_proposal("prop-r"), make_proposal("prop-l")
    store.create_request(rejected, run_id="run-1", ttl_seconds=900)
    decision = store.decide(
        "prop-r", decision="rejected", proposal_digest=rejected.digest(), actor="bob"
    )
    with pytest.raises(ApprovalError, match="rejected"):
        store.consume(decision.approval_id)
    with pytest.raises(ApprovalError, match="unknown approval"):
        store.consume("apr-forged")
    store.create_request(late, run_id="run-1", ttl_seconds=60)
    approval = approve(store, late)
    with pytest.raises(ApprovalError, match="expired"):
        store.consume(approval.approval_id, now=approval.expires_at)


def test_file_backed_store_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "recap.db"
    proposal = make_proposal()
    first = ApprovalStore(db)
    first.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(first, proposal)

    second = ApprovalStore(db)  # e.g. the API process reading the same file
    assert second.get_request("prop-1").status == ApprovalStatus.APPROVED
    assert second.get_approval(approval.approval_id) == approval


# --- waiting for a decision ----------------------------------------------------------------


async def test_waiter_is_woken_by_a_concurrent_decision(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    waiter = asyncio.create_task(store.wait_for_decision("prop-1", timeout_seconds=5))
    await asyncio.sleep(0.01)  # let the waiter start waiting
    started = time.perf_counter()
    approval = approve(store, proposal)
    assert await asyncio.wait_for(waiter, timeout=2) == approval
    assert time.perf_counter() - started < 0.4  # woken by the event, not by polling


async def test_waiter_is_woken_by_a_decision_from_another_thread(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    waiter = asyncio.create_task(store.wait_for_decision("prop-1", timeout_seconds=5))
    await asyncio.sleep(0.01)
    approval = await asyncio.to_thread(approve, store, proposal)  # like a web worker thread
    assert await asyncio.wait_for(waiter, timeout=2) == approval


async def test_waiter_returns_a_rejection(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.decide("prop-1", decision="rejected", proposal_digest=proposal.digest(), actor="bob")
    result = await store.wait_for_decision("prop-1", timeout_seconds=1)
    assert result is not None and result.decision == "rejected"


async def test_waiter_times_out_and_late_decisions_are_refused(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    assert await store.wait_for_decision("prop-1", timeout_seconds=0.05) is None
    assert store.get_request("prop-1").status == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError, match="not pending"):
        approve(store, proposal)


async def test_expire_wakes_the_waiter_and_refuses_later_decisions(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    waiter = asyncio.create_task(store.wait_for_decision("prop-1", timeout_seconds=5))
    await asyncio.sleep(0.01)  # let the waiter start waiting
    started = time.perf_counter()
    store.expire("prop-1")  # e.g. the run was cancelled while it waited
    assert await asyncio.wait_for(waiter, timeout=2) is None
    assert time.perf_counter() - started < 0.4  # woken by the event, not by polling
    assert store.get_request("prop-1").status == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError, match="not pending"):
        approve(store, proposal)


def test_expire_is_a_no_op_once_decided(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)
    store.expire("prop-1")
    assert store.get_request("prop-1").status == ApprovalStatus.APPROVED
    assert store.consume(approval.approval_id).consumed_at is not None  # still usable
    store.expire("prop-unknown")  # nothing to expire, and no error


async def test_waiting_for_an_unknown_proposal_raises(store: ApprovalStore) -> None:
    with pytest.raises(ApprovalError, match="unknown proposal"):
        await store.wait_for_decision("prop-unknown", timeout_seconds=0.05)


# --- channels ------------------------------------------------------------------------------


async def test_auto_approver_approves_the_exact_proposal_in_a_simulation(
    store: ApprovalStore,
) -> None:
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    channel = AutoApprovalChannel(store, SIM)
    await channel.notify(request)
    (approval,) = store.approvals_for("inc-1")
    assert approval.decision == "approved"
    assert approval.actor == "auto-approver[simulation]"
    assert "simulation" in channel.name


def test_auto_approver_refuses_a_real_environment(store: ApprovalStore) -> None:
    real = SIM.model_copy(update={"kind": "kubernetes", "is_simulated": False})
    with pytest.raises(ConfigError, match="simulated"):
        AutoApprovalChannel(store, real)


@pytest.mark.parametrize(
    ("answer", "decision"),
    [
        ("y", "approved"),
        (" YES ", "approved"),
        ("n", "rejected"),
        ("", "rejected"),
        ("sure", "rejected"),
    ],
)
async def test_console_channel_approves_only_on_yes(
    store: ApprovalStore, answer: str, decision: str
) -> None:
    proposal = make_proposal()
    request = store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    prompts: list[str] = []
    printed: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    await ConsoleApprovalChannel(store, input_fn=fake_input, output_fn=printed.append).notify(
        request
    )
    (approval,) = store.approvals_for("inc-1")
    assert approval.decision == decision and approval.actor == "console-user"
    assert prompts == ["Approve this exact action? [y/N] "]
    shown = "\n".join(printed)
    for expected in ["execute_rollback", "checkout", "v2.4.0", "ev-1-001", proposal.digest()[:12]]:
        assert expected in shown


async def test_console_channel_strips_control_characters_and_handles_eof(
    store: ApprovalStore,
) -> None:
    proposal = make_proposal(rationale="looks fine\x1b[2J\x1b[Happroved by admin‮")
    request = store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    printed: list[str] = []

    def no_terminal(prompt: str) -> str:
        raise EOFError

    await ConsoleApprovalChannel(store, input_fn=no_terminal, output_fn=printed.append).notify(
        request
    )
    assert store.approvals_for("inc-1")[0].decision == "rejected"
    assert not any(ch in "\n".join(printed) for ch in ("\x1b", "‮"))


async def test_external_channel_leaves_the_decision_to_the_api(store: ApprovalStore) -> None:
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    await ExternalApprovalChannel().notify(request)
    assert store.get_request("prop-1").status == ApprovalStatus.PENDING
