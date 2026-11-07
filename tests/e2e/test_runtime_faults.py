"""The MCP tool server fails: it cannot start, or its process dies in the middle of a run.

The tool server is a separate process (McpToolClient.stdio), so it can fail on its own. These
tests make the REAL server subprocess fail, on the kind-demo profile over the offline fake
cluster (as in tests/e2e/test_kind_pipeline.py), and check what a live target needs from RECAP
then: the run ends FAILED with the reason instead of hanging, nothing is written to the
cluster, no person is asked to approve a fix that can no longer be sent, and the namespace
lease is given back at once for the next run.

The failing adapters are KubernetesEnvironment subclasses named by import path, so the server
subprocess builds them from the run's configuration exactly as it builds the real adapter.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters

from recap.core.config import REPO_ROOT, EnvironmentConfig, Settings
from recap.core.models import IncidentState, RunRecord
from recap.core.observations import ActionOutcome, AuthorizedAction, LogQueryResult
from recap.environments.kubernetes import KubernetesEnvironment
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from recap.tools.mcp_client import McpToolClient
from tests.e2e.test_kind_pipeline import adapter, kind_runtime, run_with_operator

pytestmark = pytest.mark.e2e

NAMESPACE = "recap-demo"
WRITE_OPS = ("rollout_undo", "rollout_restart")
LOST = "the tool server was lost: nothing more can be read or sent; a human should take over"


class ExitsOnStart(KubernetesEnvironment):
    """The Kind adapter, except that no process but the controller's can build it: the MCP
    server subprocess exits before its MCP handshake (a broken install or configuration)."""

    def __init__(self, run_id: str, controller_pid: int = 0, **options: Any) -> None:
        if os.getpid() != controller_pid:
            raise RuntimeError("the tool server could not build its environment")
        super().__init__(run_id, **options)


class DiesOnLogs(KubernetesEnvironment):
    """The Kind adapter whose MCP server process dies (no reply, exit code 1) at the first
    log read: a crash in the middle of the investigation."""

    async def observe_logs(self, service: str, limit: int, since_minutes: int) -> LogQueryResult:
        os._exit(1)  # reads are served only by the MCP server subprocess


class DiesOnWrite(KubernetesEnvironment):
    """The Kind adapter whose MCP server process dies when the approved fix reaches it, before
    any kubectl call: the controller cannot tell whether the write was sent."""

    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        os._exit(1)  # writes are executed only by the MCP server subprocess


def failing(runtime: Runtime, adapter_class: str, **options: Any) -> Runtime:
    """The same kind-demo runtime, with its adapter replaced by one of the classes above."""
    config = runtime.settings.environment
    environment = EnvironmentConfig(
        kind=f"tests.e2e.test_runtime_faults:{adapter_class}",
        options={**config.options, **options},
    )
    return Runtime(runtime.settings.model_copy(update={"environment": environment}))


def missing_program(tmp_path: Path) -> Callable[[EnvironmentConfig, str], McpToolClient]:
    """The real MCP client, asked to launch a server program that is not there."""

    def factory(environment: EnvironmentConfig, run_id: str) -> McpToolClient:
        params = StdioServerParameters(
            command=str(tmp_path / "no-such-python"),
            args=["-m", "recap.tools.mcp_server"],
            cwd=str(REPO_ROOT),
        )
        return McpToolClient(params, transport_name="mcp-stdio")

    return factory


def categories(runtime: Runtime, record: RunRecord) -> list[str]:
    return [event.category for event in runtime.events.list_events(record.run_id)]


def assert_nothing_written_and_namespace_free(kind: Runtime, record: RunRecord) -> None:
    """The cluster's own audit (a fresh adapter on the run's local files): no write command,
    dry runs included, no write attempt, no capability verified, and no lease left behind."""
    local = adapter(kind, record.run_id)
    assert [c for c in local.state.commands() if c["op"] in WRITE_OPS] == []
    assert local.state.writes() == [] and local.state.used_nonces() == set()
    assert local.lease.holder(NAMESPACE) is None


@pytest.mark.parametrize("how", ["exits-before-the-handshake", "cannot-be-launched"])
async def test_an_mcp_server_that_cannot_start_fails_the_run_cleanly(
    settings: Settings, tmp_path: Path, how: str
) -> None:
    """The server process exits before the MCP handshake, or its program cannot be launched at
    all. The run ends FAILED at once with the reason in run.error: after the reset (the alert
    is real), before any tool call, proposal or approval request. The lease the reset took is
    given back, so the next run on the namespace starts without waiting for its expiry."""
    kind, _ = kind_runtime(settings, tmp_path)
    if how == "exits-before-the-handshake":
        runtime = failing(kind, "ExitsOnStart", controller_pid=os.getpid())
    else:
        runtime = kind
        runtime.transport_factory = missing_program(tmp_path)

    record = await run_with_operator(runtime, "kind-bad-release")

    assert record.state == IncidentState.FAILED
    assert record.outcome_note == "unexpected error; see run.error"
    expected = "Connection closed" if how == "exits-before-the-handshake" else "FileNotFoundError"
    assert record.error and expected in record.error
    assert [change.state for change in record.history] == [
        IncidentState.CREATED,
        IncidentState.FAILED,
    ]
    assert record.proposals == [] and record.approvals == [] and record.actions == []
    assert runtime.approvals.pending() == []
    seen = categories(runtime, record)
    assert C.TOOL_CALL not in seen and C.APPROVAL_REQUESTED not in seen
    assert seen.count(C.RUN_ERROR) == 1 and seen[-1] == C.RUN_SUMMARY
    assert record.episode_id is None  # a broken tool path says nothing about the incident
    assert_nothing_written_and_namespace_free(kind, record)


async def test_an_mcp_server_that_dies_while_agents_read_ends_the_run_without_a_write(
    settings: Settings, tmp_path: Path
) -> None:
    """The server process dies at the first log read. Every later call fails at once (no call
    waits for its timeout) and the run ends FAILED at the end of that investigation round,
    naming the lost server: no second round reads through the dead connection, and nothing
    is proposed, so nobody is asked to approve anything."""
    kind, _ = kind_runtime(settings, tmp_path)
    runtime = failing(kind, "DiesOnLogs")

    record = await run_with_operator(runtime, "kind-bad-release")

    assert record.state == IncidentState.FAILED and record.outcome_note == LOST
    assert record.error == "the MCP tool server stopped during the run: MCPError: Connection closed"
    events = runtime.events.list_events(record.run_id)
    results = [e.payload for e in events if e.category == C.TOOL_RESULT]
    first_log = next(i for i, r in enumerate(results) if r["tool"] == "get_service_logs")
    assert all(not r["ok"] for r in results[first_log:])
    assert {r["error"] for r in results[first_log:]} == {"MCPError: Connection closed"}
    assert len([e for e in events if e.category == C.INVESTIGATION_ROUND]) == 1
    assert record.proposals == [] and record.approvals == []
    assert runtime.approvals.pending() == []
    assert_nothing_written_and_namespace_free(kind, record)


async def test_an_mcp_server_that_dies_while_the_fix_is_sent_ends_the_run_without_a_write(
    settings: Settings, tmp_path: Path
) -> None:
    """The server process dies when the approved rollback reaches it. Whether the write was
    sent is unknown, so the oracle (which reads the cluster on its own) is given the
    stabilization window to judge; it sees no recovery. The run then ends FAILED naming the
    lost server: no new investigation through the dead connection, and no second fix for a
    person to approve. The approval was used once; nothing reached the cluster."""
    kind, _ = kind_runtime(settings, tmp_path, stabilization_timeout_seconds=1.0)
    runtime = failing(kind, "DiesOnWrite")

    record = await run_with_operator(runtime, "kind-bad-release")

    assert record.state == IncidentState.FAILED and record.outcome_note == LOST
    assert record.error == "the MCP tool server stopped during the run: MCPError: Connection closed"
    [proposal] = record.proposals
    assert (proposal.action, proposal.target) == ("execute_rollback", "backend")
    [approval] = record.approvals
    assert approval.consumed_at is not None  # single use: spent right before the send
    log = [event.kind for event in runtime.approvals.events(proposal.proposal_id)]
    assert log == ["requested", "approved", "consumed"]
    [fix] = record.actions
    assert (fix.action, fix.result) == ("execute_rollback", "error")
    assert fix.note == "tool error: MCPError: Connection closed"
    assert record.recovery is not None and not record.recovery.verified
    events = runtime.events.list_events(record.run_id)
    [recovery] = [e for e in events if e.category == C.VERIFY_RECOVERY]
    assert recovery.payload["probes"] > 1  # the oracle had the window, then the verdict stood
    assert [e.category for e in events].count(C.APPROVAL_REQUESTED) == 1
    states = [change.state for change in record.history]
    assert states[-3:] == [IncidentState.MITIGATING, IncidentState.VERIFYING, IncidentState.FAILED]
    assert runtime.approvals.pending() == []
    assert_nothing_written_and_namespace_free(kind, record)
