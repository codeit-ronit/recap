"""Tests for exact-scope approvals: lifecycle, binding, expiry, single use and channels."""

from __future__ import annotations

import asyncio
import io
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from recap.core.errors import ApprovalError, ConfigError
from recap.core.ids import utcnow
from recap.core.models import ApprovalStatus, EnvironmentInfo, MitigationProposal
from recap.safety import approvals
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


async def test_the_real_prompt_rejects_unread_without_a_terminal(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Piped input is not a person: "console-user" must always mean someone at a terminal."""
    piped = io.StringIO("y\n")  # e.g. `yes | python scripts/demo.py --approve ask`
    monkeypatch.setattr(sys, "stdin", piped)
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    await ConsoleApprovalChannel(store, output_fn=lambda *a: None).notify(request)  # real input()
    (approval,) = store.approvals_for("inc-1")
    assert approval.decision == "rejected" and approval.actor == "console-user"
    assert piped.read() == "y\n"  # the piped answer was never read
    assert (
        store.events(request.proposal_id)[-1].detail == "rejected unread: no interactive terminal"
    )


async def test_a_terminal_answer_is_logged_with_how_it_came(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

        def readline(self, *args: object) -> str:
            order.append("read")
            return super().readline()

    monkeypatch.setattr(sys, "stdin", Terminal("y\n"))
    monkeypatch.setattr(approvals, "_discard_typed_ahead", lambda: order.append("flush"))
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    await ConsoleApprovalChannel(store, output_fn=lambda *a: None).notify(request)  # real input()
    (approval,) = store.approvals_for("inc-1")
    assert approval.decision == "approved" and approval.actor == "console-user"
    assert order == ["flush", "read"]  # anything typed before the prompt was discarded first
    detail = store.events(request.proposal_id)[-1].detail
    # An instant answer is logged as one nobody could have read the proposal for.
    assert re.fullmatch(
        r"interactive terminal; answered after \d+\.\d s "
        r"\(faster than a person can read the proposal\)",
        detail,
    )


async def test_an_answer_after_reading_time_is_not_flagged(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "FAST_ANSWER_SECONDS", 0.0)
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    channel = ConsoleApprovalChannel(store, input_fn=lambda p: "y", output_fn=lambda *a: None)
    await channel.notify(request)
    assert re.fullmatch(r"answered after \d+\.\d s", store.events(request.proposal_id)[-1].detail)


def test_a_console_no_that_revokes_an_approval_keeps_its_note(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.decide(
        proposal.proposal_id,
        decision="approved",
        proposal_digest=proposal.digest(),
        actor="operator:alice",
    )
    store.decide(
        proposal.proposal_id,
        decision="rejected",
        proposal_digest=proposal.digest(),
        actor="console-user",
        note="interactive terminal; answered after 4.2 s",
    )
    revoked = store.events(proposal.proposal_id)[-1]
    assert revoked.kind == "revoked"
    assert revoked.detail == (
        "interactive terminal; answered after 4.2 s; "
        "overrides the approval by operator:alice before it was used"
    )


async def test_external_channel_leaves_the_decision_to_the_api(store: ApprovalStore) -> None:
    request = store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)
    await ExternalApprovalChannel().notify(request)
    assert store.get_request("prop-1").status == ApprovalStatus.PENDING


# --- "no" wins until the approval is used, and every step is logged (audit finding 2.4) ---


def reject(store: ApprovalStore, proposal: MitigationProposal, actor: str = "bob"):
    return store.decide(
        proposal.proposal_id, decision="rejected", proposal_digest=proposal.digest(), actor=actor
    )


def test_a_rejection_revokes_an_approval_that_was_not_used_yet(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)

    revoked = reject(store, proposal)

    assert revoked.approval_id == approval.approval_id and revoked.decision == "rejected"
    assert revoked.actor == "bob"
    assert store.get_request("prop-1").status == ApprovalStatus.REJECTED
    with pytest.raises(ApprovalError, match="rejected approval cannot be used"):
        store.consume(approval.approval_id)  # the gateway can no longer use it


def test_a_rejection_after_the_approval_was_used_is_refused(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)
    store.consume(approval.approval_id)

    with pytest.raises(ApprovalError, match="already used to send"):
        reject(store, proposal)
    assert store.get_request("prop-1").status == ApprovalStatus.APPROVED  # history is not rewritten


def test_no_approval_is_accepted_after_a_rejection(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    reject(store, proposal)

    with pytest.raises(ApprovalError, match="not pending"):
        approve(store, proposal)
    with pytest.raises(ApprovalError, match="not pending"):
        reject(store, proposal)  # deciding twice is refused too


def test_the_decision_log_records_every_step_including_refusals(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)
    with pytest.raises(ApprovalError):
        store.decide("prop-1", decision="approved", proposal_digest="f" * 64, actor="mallory")
    reject(store, proposal)
    with pytest.raises(ApprovalError):
        store.consume(approval.approval_id)

    log = store.events("prop-1")

    assert [(e.kind, e.actor) for e in log] == [
        ("requested", "recap"),
        ("approved", "alice"),
        ("refused", "mallory"),  # wrong digest
        ("revoked", "bob"),
        ("refused", "recap"),  # the gateway's attempt to use the revoked approval
    ]
    assert "digest mismatch" in log[2].detail
    assert "overrides the approval by alice" in log[3].detail
    assert [e.event_id for e in store.recent_events(limit=2)] == [log[4].event_id, log[3].event_id]


def test_the_decision_log_cannot_be_changed_or_deleted(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "recap.db"
    store = ApprovalStore(db)
    store.create_request(make_proposal(), run_id="run-1", ttl_seconds=900)

    raw = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE approval_events SET actor = 'someone else'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM approval_events")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):  # replace = delete + insert
        raw.execute(
            "INSERT OR REPLACE INTO approval_events VALUES (1, 'prop-1', 'approved', "
            "'operator:mallory', '2026-01-01T00:00:00+00:00', '', '')"
        )
    raw.close()
    assert [e.kind for e in store.events("prop-1")] == ["requested"]


def test_an_expired_request_is_logged(store: ApprovalStore) -> None:
    proposal = make_proposal()
    request = store.create_request(proposal, run_id="run-1", ttl_seconds=60)
    with pytest.raises(ApprovalError, match="expired"):
        approve(store, proposal, now=request.requested_at + timedelta(seconds=61))

    assert [e.kind for e in store.events("prop-1")] == ["requested", "expired", "refused"]


def test_two_processes_racing_revoke_against_consume_end_consistent(tmp_path: Path) -> None:
    """Whichever wins, the action ran exactly when the approval was used, never both ways."""
    import subprocess
    import sys

    db = tmp_path / "recap.db"
    store = ApprovalStore(db)
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)
    code = """
import sys
from recap.core.errors import ApprovalError
from recap.safety.approvals import ApprovalStore
db, what, approval_id, digest = sys.argv[1:]
store = ApprovalStore(db)
print("ready", flush=True)
sys.stdin.readline()
try:
    if what == "consume":
        store.consume(approval_id)
    else:
        store.decide("prop-1", decision="rejected", proposal_digest=digest, actor="console-user")
    print("won")
except ApprovalError as exc:
    print(f"lost: {exc}")
"""
    root = Path(__file__).resolve().parents[2]
    racers = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(db), what, approval.approval_id, proposal.digest()],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for what in ("consume", "revoke")
    ]
    try:
        for racer in racers:
            assert racer.stdout is not None and racer.stdout.readline().strip() == "ready"
        for racer in racers:
            racer.stdin.write("go\n")  # type: ignore[union-attr]
            racer.stdin.flush()  # type: ignore[union-attr]
        consume_out, revoke_out = (r.communicate(timeout=60)[0].strip() for r in racers)
    finally:
        for racer in racers:
            if racer.poll() is None:
                racer.kill()

    # Exactly one side wins, and the stored state agrees with the winner.
    assert sorted(o.split(":")[0] for o in (consume_out, revoke_out)) == ["lost", "won"]
    stored = store.get_approval(approval.approval_id)
    assert stored is not None
    if consume_out == "won":
        assert stored.consumed_at is not None and "already used to send" in revoke_out
    else:
        assert stored.decision == "rejected" and stored.consumed_at is None


def test_a_used_approval_is_in_the_decision_log(store: ApprovalStore) -> None:
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    store.consume(approve(store, proposal).approval_id)

    assert [e.kind for e in store.events("prop-1")] == ["requested", "approved", "consumed"]


def test_a_decision_waits_for_another_writer_instead_of_reading_stale_state(
    tmp_path: Path,
) -> None:
    """BEGIN IMMEDIATE: while another process holds the write lock and uses the approval, a
    consume here waits for it, then refuses cleanly; it never acts on a stale read."""
    import sqlite3
    import threading

    db = tmp_path / "recap.db"
    store = ApprovalStore(db)
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-1", ttl_seconds=900)
    approval = approve(store, proposal)

    other = sqlite3.connect(db, timeout=10, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # another process takes the write lock first
    outcome: list[BaseException | None] = []

    def consume() -> None:
        try:
            store.consume(approval.approval_id)
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    time.sleep(0.3)  # the consume is now waiting for the lock
    other.execute(
        "UPDATE approvals SET consumed_at = ? WHERE approval_id = ?",
        (utcnow().isoformat(), approval.approval_id),
    )
    other.execute("COMMIT")
    worker.join(timeout=15)
    other.close()

    assert len(outcome) == 1 and isinstance(outcome[0], ApprovalError), outcome
    assert "already used" in str(outcome[0])
