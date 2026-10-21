"""Audit 2.4 (part 1): a human "no" must be final, even if another approver raced in first.

What the audit found
--------------------
A proposal that waits for approval can be answered from two places at the same time: the
operator at the terminal (ConsoleApprovalChannel) and anyone using the HTTP API
(POST /approvals/{id}). The approval store keeps only the FIRST decision for a request.
So when an API client approves while the operator is still reading the console prompt,
the operator's later "n" is refused by the store ("approval request is approved, not
pending"). The controller only writes that refusal to the trace, then picks up the stored
approval and executes the rollback: the operator said no, and the action still ran.

How these tests reproduce it
----------------------------
1. A real run of the 'bad-deployment' scenario (real controller, real MCP subprocess,
   simulated environment). The console's "keyboard" (input_fn) first approves the pending
   proposal the way an API client would, then types "n". Correct behaviour: the run ends
   BLOCKED, nothing is executed and the approval is never used.
2. The same race at the store level, across two operating-system processes that share one
   SQLite file (like the API server and the CLI): the other process approves, this process
   then rejects. Correct behaviour: the rejection wins and the approval cannot be used.
3. A guard that already passes: two processes racing to use ONE approval -> exactly one
   succeeds, because single use is enforced inside SQLite.

Status: tests 1 and 2 were strict xfails in M0. M1 fixed the store: a rejection now also
revokes an approval that was not used yet ("no" wins until the approval is used), every
transition is a compare-and-swap inside one SQLite write transaction, and every step is
written to an append-only decision log. They now pass and stay as regression tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from recap.core.config import Settings
from recap.core.errors import ApprovalError
from recap.core.models import ApprovalStatus, IncidentState, MitigationProposal, RunConfig
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from recap.safety.approvals import ApprovalStore, ConsoleApprovalChannel
from recap.telemetry.tracer import TraceCategory as C

REPO_ROOT = Path(__file__).resolve().parents[2]  # child processes run from here
MANUAL = RunConfig(approval_mode="manual", memory_write=False)

# Code for a SEPARATE Python process (like the API server): approve one pending request in
# the shared SQLite file and print the new approval's ID.
APPROVE_FROM_ANOTHER_PROCESS = """
import sys
from recap.safety.approvals import ApprovalStore

db_path, proposal_id, digest = sys.argv[1:]
approval = ApprovalStore(db_path).decide(
    proposal_id, decision="approved", proposal_digest=digest, actor="api-server"
)
print(approval.approval_id)
"""

# Code for a SEPARATE Python process that tries to use (consume) one approval. It opens the
# store, says "ready", and waits for "go" on stdin, so two of them can be released together.
CONSUME_FROM_ANOTHER_PROCESS = """
import sys
from recap.core.errors import ApprovalError
from recap.safety.approvals import ApprovalStore

store = ApprovalStore(sys.argv[1])
print("ready", flush=True)
sys.stdin.readline()  # block until the test says "go"
try:
    store.consume(sys.argv[2])
    print("consumed")
except ApprovalError as exc:
    print(f"refused: {exc}")
"""


# --- helpers ----------------------------------------------------------------------------------


def make_proposal() -> MitigationProposal:
    """A rollback proposal like the one the demo produces for 'bad-deployment'."""
    return MitigationProposal(
        proposal_id="prop-race-001",
        incident_id="inc-race-001",
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="errors started right after v2.4.1",
        evidence_ids=["ev-1-001"],
    )


def with_short_approval_ttl(settings: Settings, seconds: int) -> Settings:
    """Settings whose approval requests expire after `seconds`, so a test can never hang."""
    approvals = settings.budget.approvals.model_copy(update={"ttl_seconds": seconds})
    budget = settings.budget.model_copy(update={"approvals": approvals})
    return settings.model_copy(update={"budget": budget})


def mitigation_events(runtime: Runtime, run_id: str) -> list[object]:
    """Trace events that RECAP writes when a write action is sent to the environment."""
    return [e for e in runtime.events.list_events(run_id) if e.category == C.MITIGATION]


def run_in_another_process(code: str, *args: str) -> str:
    """Run `code` in a fresh Python process and return what it printed."""
    done = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# --- 1. the console "n" after another party's approval ----------------------------------------


@pytest.mark.e2e
async def test_an_operator_no_stops_the_run_even_if_someone_else_approved_first(
    settings: Settings,
) -> None:
    """The operator typed 'n' after an API client approved: nothing may be executed."""
    # A short expiry only bounds the test: if a fix ever made the run wait, it stops in 5 s.
    runtime = Runtime(with_short_approval_ttl(settings, seconds=5))
    api_attempts: list[str] = []

    def approve_via_api_then_type_no(prompt: str) -> str:
        # The console runs this "keyboard" in a worker thread while the run waits. Before the
        # operator answers, another party approves the same proposal through the shared
        # store, exactly as POST /approvals/{id} would.
        request = runtime.approvals.pending()[0]
        try:
            runtime.approvals.decide(
                request.proposal_id,
                decision="approved",
                proposal_digest=request.proposal_digest,
                actor="api-user",
            )
            api_attempts.append("accepted")
        except ApprovalError as exc:  # a fixed store may refuse it; the operator still answers
            api_attempts.append(f"refused: {exc}")
        return "n"  # ...and then the operator says no

    channel = ConsoleApprovalChannel(
        runtime.approvals, input_fn=approve_via_api_then_type_no, output_fn=lambda *a: None
    )
    record = await runtime.run_incident("bad-deployment", MANUAL, approval_channel=channel)

    assert len(api_attempts) == 1  # the console really asked, so the race really happened
    assert record.state == IncidentState.BLOCKED, record.outcome_note
    # Nothing was executed: no write in RECAP's trace and none in the environment's own log.
    assert mitigation_events(runtime, record.run_id) == []
    sandbox = create_environment(runtime.settings.environment, record.run_id)
    assert sandbox.store.load()["action_log"] == []  # type: ignore[attr-defined]
    # The approval that raced in was never used.
    assert record.incident is not None
    stored = runtime.approvals.approvals_for(record.incident.incident_id)
    assert all(approval.consumed_at is None for approval in stored)
    # The "no" is logged with how it came, in the decision log and in the trace.
    log = runtime.approvals.events(stored[0].proposal_id)
    [revoked] = [e for e in log if e.kind == "revoked"]
    assert "answered after" in revoked.detail and "overrides the approval" in revoked.detail
    decided = [
        e for e in runtime.events.list_events(record.run_id) if e.category == "approval.decided"
    ]
    assert decided and "answered after" in decided[-1].payload.get("note", "")


# --- 2. cross-process: a later rejection must win --------------------------------------------


def test_a_rejection_overrides_an_approval_written_by_another_process(tmp_path: Path) -> None:
    """Another process approved first; the later rejection in this process must still win."""
    db = tmp_path / "recap.db"  # one SQLite file shared by both processes
    store = ApprovalStore(db)
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-race-001", ttl_seconds=900)

    approval_id = run_in_another_process(
        APPROVE_FROM_ANOTHER_PROCESS, str(db), proposal.proposal_id, proposal.digest()
    )

    # The operator in this process says no after that approval was written.
    refusal = ""
    try:
        store.decide(
            proposal.proposal_id,
            decision="rejected",
            proposal_digest=proposal.digest(),
            actor="console-user",
        )
    except ApprovalError as exc:  # kept only to show it in the failure message below
        refusal = str(exc)

    # What is stored is the rejection (read back by a fresh store on the same file)...
    request = ApprovalStore(db).get_request(proposal.proposal_id)
    assert request is not None
    assert request.status == ApprovalStatus.REJECTED, f"the rejection was refused: {refusal}"
    # ...and the earlier approval can no longer authorize the action.
    with pytest.raises(ApprovalError):
        store.consume(approval_id)


# --- 3. guard: one approval, two processes, one use -------------------------------------------


def test_two_processes_racing_to_use_one_approval_cannot_both_succeed(tmp_path: Path) -> None:
    """Guard: two processes using the same approval at the same moment -> exactly one wins."""
    db = tmp_path / "recap.db"
    store = ApprovalStore(db)
    proposal = make_proposal()
    store.create_request(proposal, run_id="run-race-001", ttl_seconds=900)
    approval = store.decide(
        proposal.proposal_id,
        decision="approved",
        proposal_digest=proposal.digest(),
        actor="alice",
    )

    racers = [
        subprocess.Popen(
            [sys.executable, "-c", CONSUME_FROM_ANOTHER_PROCESS, str(db), approval.approval_id],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for racer in racers:  # wait until both processes have opened the store
            assert racer.stdout is not None and racer.stdin is not None
            assert racer.stdout.readline().strip() == "ready", racer.communicate()[1]
        for racer in racers:  # then release both at (almost) the same instant
            racer.stdin.write("go\n")  # type: ignore[union-attr]
            racer.stdin.flush()  # type: ignore[union-attr]
        outputs = [racer.communicate(timeout=60)[0].strip() for racer in racers]
    finally:
        for racer in racers:  # never leave a child process behind, even on failure
            if racer.poll() is None:
                racer.kill()

    # Each output is "consumed" or "refused: <reason>"; exactly one of each is allowed.
    assert sorted(out.split(":")[0] for out in outputs) == ["consumed", "refused"], outputs
    used = store.get_approval(approval.approval_id)
    assert used is not None and used.consumed_at is not None
