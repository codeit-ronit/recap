"""A run's process killed outright (SIGKILL: no cleanup code runs) while it waits for approval.

A cancelled run expires its approval request and gives its namespace back
(tests/e2e/test_kind_pipeline.py). A killed process can do neither, so what it leaves behind
must be unusable on its own:

  * nothing was written to the cluster, and no write can be made for the dead run: its
    signing key lived only in its memory, and the cluster's adapter accepts capabilities
    signed by that key alone;
  * its tool server process does not outlive it;
  * its approval request stays pending in the shared store (the store cannot tell that the
    run died) until its TTL, and from then on it cannot be approved;
  * its namespace lease is given back by nobody: a new run is refused until the lease's own
    expiry, and runs normally after it.

The child process runs what a server runs: a Runtime on the kind-demo profile over the offline
fake, with a short approval TTL and lease so the test takes seconds.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from recap.core.config import REPO_ROOT, Settings
from recap.core.errors import ApprovalError
from recap.core.ids import new_id
from recap.core.models import ApprovalRequest, ApprovalStatus, IncidentState
from recap.core.observations import ActionOutcome, AuthorizedAction
from recap.environments.kubernetes.state import LeaseHeld
from recap.runtime import Runtime
from recap.safety.capabilities import CapabilityIssuer
from recap.tools.mcp_client import McpToolClient
from tests.e2e.test_kind_pipeline import OPERATOR, adapter, kind_runtime, run_with_operator
from tests.helpers import sign

pytestmark = pytest.mark.e2e

NAMESPACE = "recap-demo"
WRITE_OPS = ("rollout_undo", "rollout_restart")
TTL_SECONDS = 3
LEASE_SECONDS = 5.0

# The child: one incident on the given environment, waiting for a person who never answers.
CHILD = """
import asyncio, sys
from recap.core.config import EnvironmentConfig, load_settings
from recap.core.models import RunConfig
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel

data_dir, environment, ttl, run_id = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
settings = load_settings(data_dir=data_dir, target="kind-demo")
approvals = settings.budget.approvals.model_copy(update={"ttl_seconds": ttl})
settings = settings.model_copy(
    update={
        "environment": EnvironmentConfig.model_validate_json(environment),
        "budget": settings.budget.model_copy(update={"approvals": approvals}),
    }
)
asyncio.run(
    Runtime(settings).run_incident(
        "kind-bad-release",
        RunConfig(memory_write=False),
        approval_channel=ExternalApprovalChannel(),
        run_id=run_id,
    )
)
"""


def start_child(runtime: Runtime, run_id: str) -> subprocess.Popen[bytes]:
    """The run in a process of its own, on the same data folder and environment as `runtime`."""
    settings = runtime.settings
    args = [
        str(settings.paths.data_dir),
        settings.environment.model_dump_json(),
        str(TTL_SECONDS),
        run_id,
    ]
    return subprocess.Popen([sys.executable, "-c", CHILD, *args], cwd=REPO_ROOT)


async def wait_for_request(
    runtime: Runtime, child: subprocess.Popen[bytes], run_id: str
) -> ApprovalRequest:
    """The run's approval request, once its checkpoint says it waits for it (the request is
    stored a moment before that checkpoint: a kill in between would leave a different state)."""
    for _ in range(600):
        record = runtime.runs.get(run_id)
        if record is not None and record.pending_approval is not None:
            return record.pending_approval
        assert child.poll() is None, "the run's process ended before asking for approval"
        await asyncio.sleep(0.05)
    raise AssertionError("no approval request appeared")


def tool_servers(run_id: str) -> list[int]:
    """Process IDs of MCP tool servers started for `run_id` (the SDK gives each server its own
    session, so it is found by its command line; `ps` reads the same on macOS and Linux)."""
    listing = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=", "-o", "command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        int(line.split(None, 1)[0])
        for line in listing.splitlines()
        if "recap.tools.mcp_server" in line and f"--run-id {run_id}" in line
    ]


async def wait_until(moment: float) -> None:
    """Sleep until time.time() is past `moment` (a POSIX timestamp)."""
    await asyncio.sleep(max(0.0, moment - time.time()) + 0.1)


async def test_a_run_killed_while_waiting_for_approval_leaves_nothing_usable(
    settings: Settings, tmp_path: Path
) -> None:
    """SIGKILL while the run waits for its approval. Nothing reached the cluster, and its tool
    server exits with it. The checkpoint stays at AWAITING_APPROVAL with the request pending:
    nobody is left to expire it. A write for the dead run carrying a capability signed by any
    other key is refused by the cluster's adapter (the run's own key died with it); a new run
    on the namespace is refused while the dead run's lease lasts. Once the request's TTL is
    over it cannot be approved, and once the lease's expiry has passed the next run resolves."""
    runtime, _ = kind_runtime(settings, tmp_path, lease_seconds=LEASE_SECONDS)
    run_id = new_id("run")  # unique: another test session may be running this test too
    child = start_child(runtime, run_id)
    try:
        request = await wait_for_request(runtime, child, run_id)
        assert len(tool_servers(run_id)) == 1  # the run's tool server, waiting with it
        child.send_signal(signal.SIGKILL)  # no finally block, no cleanup code runs
        child.wait()
        for _ in range(100):  # its server sees its stdin close and exits on its own
            if not tool_servers(run_id):
                break
            await asyncio.sleep(0.05)
        assert tool_servers(run_id) == []

        local = adapter(runtime, run_id)
        assert [c for c in local.state.commands() if c["op"] in WRITE_OPS] == []
        assert local.state.writes() == [] and local.state.used_nonces() == set()
        record = runtime.runs.get(run_id)
        assert record is not None and record.state == IncidentState.AWAITING_APPROVAL
        assert record.pending_approval is not None
        assert record.pending_approval.proposal_id == request.proposal_id
        stored = runtime.approvals.get_request(request.proposal_id)
        assert stored is not None and stored.status == ApprovalStatus.PENDING
        lease = local.lease.holder(NAMESPACE)
        assert lease is not None and lease.run_id == run_id
        with pytest.raises(LeaseHeld, match=f"leased by run {run_id}"):
            await runtime.run_incident("kind-bad-release")

        # A write for the dead run, through its own tool server, with a capability for exactly
        # this rollback signed by a key the run never bound: refused, and nothing changed.
        rollback = AuthorizedAction(
            action="rollback",
            target="backend",
            params={"to_version": "v1.0.0"},
            approval_id="apr-orphaned",
            proposal_digest=request.proposal_digest,
        )
        forged = sign(CapabilityIssuer(run_id, request.incident_id), rollback)
        args = {
            "service": "backend",
            "to_version": "v1.0.0",
            "approval_id": rollback.approval_id,
            "proposal_digest": rollback.proposal_digest,
            "capability": forged.capability,
        }
        async with McpToolClient.stdio(runtime.settings.environment, run_id) as client:
            answer = await client.call_tool("execute_rollback", args, 10.0)
        outcome = ActionOutcome.model_validate(answer.structured)
        assert not outcome.ok
        assert outcome.message == "not authorized: the capability's signature is not valid"
        assert [c for c in local.state.commands() if c["op"] in WRITE_OPS] == []
        assert [w["outcome"] for w in local.state.writes()] == ["refused"]
        assert local.state.used_nonces() == set()

        await wait_until(request.expires_at.timestamp())
        with pytest.raises(ApprovalError, match="expired before a decision was made"):
            runtime.approvals.decide(
                request.proposal_id,
                decision="approved",
                proposal_digest=request.proposal_digest,
                actor=OPERATOR,
            )
        stored = runtime.approvals.get_request(request.proposal_id)
        assert stored is not None and stored.status == ApprovalStatus.EXPIRED

        await wait_until(lease.expires_at.timestamp())
        following = await run_with_operator(runtime, "kind-bad-release")
        assert following.state == IncidentState.RESOLVED, following.outcome_note
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for pid in tool_servers(run_id):  # never leave a server behind, even on a failure
            os.kill(pid, signal.SIGKILL)
