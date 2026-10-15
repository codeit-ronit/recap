"""AUDIT-2.5 (MCP execution authority): the tool server must refuse writes nobody authorized.

What the audit found, in simple words
-------------------------------------
RECAP's safety gateway (recap/tools/gateway.py) checks every write: the policy, an exact
human approval, and single use. Only then does it call a write tool on the MCP server. But
the server itself trusts whoever calls it. Its write tools (execute_rollback, restart_service,
delete_namespace in recap/tools/mcp_server.py) accept approval_id="" and proposal_digest=""
(or any made-up values) and pass the action straight to TargetEnvironment.execute_authorized,
which applies it. The two fields are "carried for the audit trail only". So anything that can
speak MCP to the server, for example a second server process started with

    python -m recap.tools.mcp_server --env-config <json> --run-id <existing run id>

can change a run's sandbox (the state file <state_dir>/<run_id>.db) without the gateway or a
human ever agreeing. The audit rolled checkout back and deleted the namespace this way, and the
recovery oracle then saw every service Deleted.

How these tests reproduce it
----------------------------
Each test prepares a sandbox, writes down the facts a write could change (running versions,
deployment history, restart events, service statuses, the simulated clock) by asking the
environment itself, sends a write that RECAP's gateway did NOT authorize, and then checks that
(1) the write was refused and (2) the facts did not change. "Refused" means either a tool error
from the server or an ActionOutcome with ok=False from the environment: the tests do not care
which layer says no, or how the fix proves authority (a signed token, an approval lookup, ...).
One more test sends each read tool an argument it does not define (a smuggled approval_id):
the server must reject it instead of silently dropping it.

Today every such write is applied and every extra argument is dropped, so those tests fail
with an AssertionError that shows what the server accepted. They are marked
xfail(strict=True) until M1 fixes the boundary. One control test (no xfail) checks that a
write RECAP's gateway did approve still reaches the sandbox, so a fix cannot simply switch
all writes off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from recap.core.config import REPO_ROOT, EnvironmentConfig, Settings
from recap.core.interfaces import TargetEnvironment
from recap.core.models import IncidentState, RawToolResult, RunConfig
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from recap.tools.mcp_client import McpToolClient
from recap.tools.mcp_server import build_mcp_server

RUN_ID = "run-audit25"
SCENARIOS_DIR = REPO_ROOT / "fixtures" / "scenarios"
TIMEOUT_SECONDS = 10.0
GOOD_VERSION = "v2.4.0"  # checkout's healthy release in bad-deployment.yaml (it runs bad v2.4.1)
AUTO_NO_MEMORY = RunConfig(approval_mode="auto", memory_write=False)
NO_APPROVAL = {"approval_id": "", "proposal_digest": ""}  # what the audit sent
FORGED_APPROVAL = {"approval_id": "apr-forged", "proposal_digest": "0" * 64}

# The three write tools, each with VALID arguments: the only thing missing is authority.
WRITES = [
    pytest.param(
        "execute_rollback", {"service": "checkout", "to_version": GOOD_VERSION}, id="rollback"
    ),
    pytest.param("restart_service", {"service": "checkout"}, id="restart"),
    pytest.param("delete_namespace", {"namespace": "shop"}, id="delete-namespace"),
]

# The seven read tools, each with valid arguments.
READS = [
    pytest.param("list_services", {}, id="list_services"),
    pytest.param("get_service_health", {"service": "checkout"}, id="get_service_health"),
    pytest.param("get_service_logs", {"service": "checkout"}, id="get_service_logs"),
    pytest.param(
        "get_service_metrics",
        {"service": "checkout", "metric": "error_rate"},
        id="get_service_metrics",
    ),
    pytest.param("get_recent_deployments", {}, id="get_recent_deployments"),
    pytest.param("get_k8s_like_events", {}, id="get_k8s_like_events"),
    pytest.param("propose_rollback", {"service": "checkout"}, id="propose_rollback"),
]


@dataclass
class SandboxFacts:
    """What a write could change in the simulated shop, as the environment itself reports it."""

    versions: dict[str, str]  # running version per service: a rollback changes one
    deployments: list[tuple[str, str, str]]  # (service, version, status): a rollback adds one
    restart_events: int  # Kubernetes-style "Killing" events: every restart adds one
    statuses: dict[str, str]  # every service turns "Deleted" after delete_namespace
    clock: datetime  # every executed write moves the simulated clock forward


def sandbox_config(state_dir: Path) -> EnvironmentConfig:
    """The simulator config RECAP gives its own MCP server, with the state under `state_dir`."""
    options = {
        "scenarios_dir": str(SCENARIOS_DIR),
        "state_dir": str(state_dir),
        "tool_latency_ms": 0,
        "write_latency_ms": 0,
    }
    return EnvironmentConfig(kind="simulated", options=options)


async def broken_sandbox(state_dir: Path) -> TargetEnvironment:
    """A sandbox reset to bad-deployment (checkout runs the bad v2.4.1), as the controller does."""
    environment = create_environment(sandbox_config(state_dir), RUN_ID)
    await environment.reset("bad-deployment", seed=11)
    return environment


async def sandbox_facts(environment: TargetEnvironment) -> SandboxFacts:
    """Read the facts through the environment's own methods (the recovery oracle's view)."""
    services = (await environment.observe_services()).services
    deployments = (await environment.observe_deployments(None, 100)).deployments
    events = (await environment.observe_events(None, 500)).events
    health = await environment.health_check()
    return SandboxFacts(
        versions={service.name: service.version for service in services},
        deployments=[(d.service, d.version, d.status) for d in deployments],
        restart_events=len([event for event in events if event.reason == "Killing"]),
        statuses={name: snapshot.status for name, snapshot in health.services.items()},
        clock=health.checked_at,
    )


async def call_server_directly(
    environment: TargetEnvironment, tool: str, args: dict[str, object]
) -> RawToolResult:
    """Call one tool on RECAP's MCP server for `environment`, with no gateway in between."""
    # In-process MCP is still real MCP (JSON-RPC and the initialize handshake), only without a
    # subprocess: exactly what any MCP client could send to the server.
    async with McpToolClient.in_process(build_mcp_server(environment)) as client:
        return await client.call_tool(tool, args, TIMEOUT_SECONDS)


def refused(result: RawToolResult) -> bool:
    """True if the write was turned down, by the server (tool error) or by the environment."""
    if not result.ok:
        return True  # the MCP call failed, e.g. a tool error such as "not authorized"
    return (result.structured or {}).get("ok") is False  # an ActionOutcome with ok=False


# --- writes sent straight to the server (in-process MCP, no gateway) -----------------------


@pytest.mark.xfail(
    strict=True, reason="AUDIT-2.5: MCP write tools run with no approval at all; fixed in M1"
)
@pytest.mark.parametrize(
    "approval",
    [pytest.param({}, id="fields-omitted"), pytest.param(NO_APPROVAL, id="fields-empty")],
)
@pytest.mark.parametrize(("tool", "args"), WRITES)
async def test_a_write_without_an_approval_is_refused_and_changes_nothing(
    tmp_path: Path, tool: str, args: dict[str, object], approval: dict[str, str]
) -> None:
    """A write tool called with no approval must say no and leave the sandbox as it was."""
    sandbox = await broken_sandbox(tmp_path / "sandbox")
    before = await sandbox_facts(sandbox)

    result = await call_server_directly(sandbox, tool, {**args, **approval})

    assert refused(result), f"{tool} ran without any approval: {result.structured}"
    assert await sandbox_facts(sandbox) == before


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.5: MCP write tools accept a made-up approval id and digest; fixed in M1",
)
@pytest.mark.parametrize(("tool", "args"), WRITES)
async def test_a_write_with_a_forged_approval_is_refused_and_changes_nothing(
    tmp_path: Path, tool: str, args: dict[str, object]
) -> None:
    """A made-up approval id and an all-zero digest must not authorize anything."""
    sandbox = await broken_sandbox(tmp_path / "sandbox")
    before = await sandbox_facts(sandbox)

    result = await call_server_directly(sandbox, tool, {**args, **FORGED_APPROVAL})

    assert refused(result), f"{tool} ran with a forged approval: {result.structured}"
    assert await sandbox_facts(sandbox) == before


# --- a second, independently started server process ----------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.5: a second MCP server process can write to a run's sandbox; fixed in M1",
)
async def test_a_second_server_process_for_the_same_run_cannot_write(tmp_path: Path) -> None:
    """A rogue `python -m recap.tools.mcp_server` for an existing run must not change it."""
    state_dir = tmp_path / "sandbox"
    sandbox = await broken_sandbox(state_dir)
    before = await sandbox_facts(sandbox)

    # McpToolClient.stdio runs the audit's command line in a new process:
    #   python -m recap.tools.mcp_server --env-config <this config> --run-id <this run id>
    # That process shares nothing with this test except the sandbox state file.
    async with McpToolClient.stdio(sandbox_config(state_dir), RUN_ID) as rogue:
        rollback_args = {"service": "checkout", "to_version": GOOD_VERSION, **NO_APPROVAL}
        rollback = await rogue.call_tool("execute_rollback", rollback_args, TIMEOUT_SECONDS)
        delete_args = {"namespace": "shop", **NO_APPROVAL}
        delete = await rogue.call_tool("delete_namespace", delete_args, TIMEOUT_SECONDS)

    assert refused(rollback), f"the second process rolled checkout back: {rollback.structured}"
    assert refused(delete), f"the second process deleted the namespace: {delete.structured}"
    # Oracle channel: this process reads the shared state file itself, without MCP.
    assert await sandbox_facts(sandbox) == before


# --- approval ids are public, so they must not work as keys --------------------------------
# A finished run's record (GET /runs/{run_id}) and its trace events show each approval's id
# and proposal digest, so anyone who can read the dashboard knows them.


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.5: a public approval id unlocks any write at the server; fixed in M1",
)
async def test_an_approval_from_a_finished_run_cannot_unlock_a_different_write(
    settings: Settings,
) -> None:
    """The real approval of RECAP's rollback must not let another process delete the namespace."""
    record = await Runtime(settings).run_incident("bad-deployment", AUTO_NO_MEMORY)
    approval = record.approvals[-1]  # approved for "roll checkout back to v2.4.0" only
    sandbox = create_environment(settings.environment, record.run_id)
    before = await sandbox_facts(sandbox)

    # A second server process with RECAP's own --env-config and the finished run's --run-id.
    async with McpToolClient.stdio(settings.environment, record.run_id) as rogue:
        stolen = {"approval_id": approval.approval_id, "proposal_digest": approval.proposal_digest}
        args = {"namespace": "shop", **stolen}
        result = await rogue.call_tool("delete_namespace", args, TIMEOUT_SECONDS)

    assert refused(result), f"a rollback approval deleted the namespace: {result.structured}"
    assert await sandbox_facts(sandbox) == before


@pytest.mark.xfail(
    strict=True,
    reason="AUDIT-2.5: a used approval can be replayed straight to the server; fixed in M1",
)
async def test_a_used_approval_cannot_be_replayed_straight_to_the_server(
    settings: Settings,
) -> None:
    """Re-sending a restart RECAP already executed, with its real approval, must not run again."""
    record = await Runtime(settings).run_incident("resource-pressure", AUTO_NO_MEMORY)
    proposal, approval = record.proposals[-1], record.approvals[-1]
    assert approval.consumed_at is not None  # RECAP's gateway already used this approval
    sandbox = create_environment(settings.environment, record.run_id)
    before = await sandbox_facts(sandbox)

    # The same tool and arguments the gateway sent, from a second server process.
    async with McpToolClient.stdio(settings.environment, record.run_id) as rogue:
        replay = {
            "service": proposal.target,
            "approval_id": approval.approval_id,
            "proposal_digest": approval.proposal_digest,
        }
        result = await rogue.call_tool(proposal.action, replay, TIMEOUT_SECONDS)

    assert refused(result), f"the replayed approval restarted again: {result.structured}"
    assert await sandbox_facts(sandbox) == before


# --- the server's own argument checks ------------------------------------------------------


@pytest.mark.xfail(
    strict=True, reason="AUDIT-2.5: MCP read tools silently ignore unknown arguments; fixed in M1"
)
@pytest.mark.parametrize(("tool", "args"), READS)
async def test_a_read_tool_rejects_an_argument_it_does_not_define(
    tmp_path: Path, tool: str, args: dict[str, object]
) -> None:
    """A smuggled extra argument (here approval_id) must be an error, not silently dropped."""
    sandbox = await broken_sandbox(tmp_path / "sandbox")

    plain = await call_server_directly(sandbox, tool, args)
    smuggled = await call_server_directly(sandbox, tool, {**args, "approval_id": "apr-stolen"})

    assert plain.ok, plain.error  # the same call without the extra argument works
    assert not smuggled.ok, f"{tool} accepted an argument it does not define"


# --- control: the legitimate path must keep working ------------------------------------------


async def test_control_a_write_approved_through_recaps_gateway_still_reaches_the_sandbox(
    settings: Settings,
) -> None:
    """Control: RECAP's own approved rollback still changes the sandbox (the fix must keep this)."""
    record = await Runtime(settings).run_incident("bad-deployment", AUTO_NO_MEMORY)

    assert record.state == IncidentState.RESOLVED
    assert record.approvals[-1].consumed_at is not None  # the gateway used the approval
    facts = await sandbox_facts(create_environment(settings.environment, record.run_id))
    assert facts.versions["checkout"] == GOOD_VERSION
    assert ("checkout", GOOD_VERSION, "rollback") in facts.deployments
