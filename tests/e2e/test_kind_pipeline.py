"""The whole pipeline on the Kind adapter, through the real MCP server subprocess, offline.

The kind-demo target profile with one change: `client: fake` (the offline fake cluster,
synthetic data; recap/environments/kubernetes/fake.py). Everything else is real: the
controller, the agents (scripted policy), the MCP server subprocess building its own
KubernetesEnvironment from the config, the gateway, the policy (policy-kind-v1), an
approval decided through the External channel as an operator would in the dashboard, the
signed capability, the adapter's write path and its oracle. Like
tests/e2e/test_environment_swap.py, it proves the wiring; what only a real cluster can show
(RBAC, the admission policy, real rollout times) is in tests/e2e/test_kind_live.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import REPO_ROOT, Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.models import IncidentState, RunConfig, RunRecord
from recap.core.observations import ActionOutcome, HealthSnapshot
from recap.environments.kubernetes import KubernetesEnvironment
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel
from recap.telemetry.tracer import TraceCategory as C
from recap.tools.mcp_client import McpToolClient
from tests.helpers import capability_args
from tests.unit.test_kind_preflight import write_credentials

pytestmark = pytest.mark.e2e  # every test here runs a real incident end to end

OPERATOR = "operator:kind-e2e"
CAPABILITY = re.compile(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")
RUN_LIMIT_SECONDS = 120.0


def kind_runtime(settings: Settings, tmp_path: Path) -> tuple[Runtime, Path]:
    """The kind-demo profile on the offline fake, with a real (synthetic) credentials folder
    so both processes run the cluster-pinning checks."""
    del settings  # loaded for its environment variables (latency 0, operator token, ...)
    credentials = write_credentials(tmp_path / "private" / "kind")
    kind = load_settings(data_dir=tmp_path / "data", target="kind-demo")
    options = dict(kind.environment.options)
    options.update(
        client="fake",
        fake_data_dir=str(REPO_ROOT / "tests" / "fixtures" / "kind"),
        credentials_dir=str(credentials),
        probe_round_gap_seconds=0.0,
        alert_poll_seconds=0.02,
        stabilization_poll_seconds=0.2,
        stabilization_timeout_seconds=20.0,
    )
    environment = kind.environment.model_copy(update={"options": options})
    return Runtime(kind.model_copy(update={"environment": environment})), credentials


async def run_with_operator(runtime: Runtime, scenario: str) -> RunRecord:
    """Run one incident; approve each exact request through the API's channel, as a person."""
    task = asyncio.create_task(
        runtime.run_incident(
            scenario, RunConfig(memory_write=False), approval_channel=ExternalApprovalChannel()
        )
    )
    async with asyncio.timeout(RUN_LIMIT_SECONDS):
        while not task.done():
            for request in runtime.approvals.pending():
                runtime.approvals.decide(
                    request.proposal_id,
                    decision="approved",
                    proposal_digest=request.proposal_digest,
                    actor=OPERATOR,
                )
            await asyncio.sleep(0.05)
    return task.result()


def adapter(runtime: Runtime, run_id: str) -> KubernetesEnvironment:
    """A fresh adapter on the run's local files (what an operator would inspect)."""
    built = KubernetesEnvironment(run_id, **runtime.settings.environment.options)
    return built


def everything_written(runtime: Runtime, record: RunRecord) -> str:
    """The run as stored and traced: its record, every event, its JSONL trace, its approvals."""
    events = runtime.events.list_events(record.run_id)
    jsonl = runtime.settings.paths.traces_dir / f"{record.run_id}.jsonl"
    stored = runtime.runs.get(record.run_id)
    parts: list[Any] = [
        record.model_dump(mode="json"),
        stored.model_dump(mode="json") if stored else None,
        [event.model_dump(mode="json") for event in events],
        [
            a.model_dump(mode="json")
            for a in runtime.approvals.approvals_for(record.incident.incident_id)
        ]
        if record.incident
        else [],
    ]
    return json.dumps(parts) + (jsonl.read_text(encoding="utf-8") if jsonl.exists() else "")


async def test_a_bad_release_is_rolled_back_after_an_operators_approval(
    settings: Settings, tmp_path: Path
) -> None:
    runtime, credentials = kind_runtime(settings, tmp_path)
    assert runtime.environment_info().is_simulated is False

    record = await run_with_operator(runtime, "kind-bad-release")

    assert record.state == IncidentState.RESOLVED, record.outcome_note
    assert record.outcome_note == "oracle verified recovery (kind)"
    assert record.environment and record.environment.kind == "kind"
    assert record.environment.is_simulated is False
    assert record.versions["transport"] == "mcp-stdio"
    assert record.recovery and record.recovery.oracle_name == "kind-health-oracle"
    assert record.recovery.healthy_before is False and record.recovery.verified
    proposal = record.proposals[-1]
    assert (proposal.action, proposal.target, proposal.params) == (
        "execute_rollback",
        "backend",
        {"to_version": "v1.0.0"},
    )
    [approval] = record.approvals
    assert approval.actor == OPERATOR and approval.consumed_at is not None
    assert [(a.action, a.target, a.result) for a in record.actions] == [
        ("execute_rollback", "backend", "recovered")
    ]
    # Exactly one write reached the cluster: a server-side dry run, then the undo, by the
    # writer identity, under this approval, with its capability's nonce recorded once.
    local = adapter(runtime, record.run_id)
    undo = [c for c in local.state.commands() if c["op"] == "rollout_undo"]
    assert [("--dry-run=server" in c["argv"], c["identity"]) for c in undo] == [
        (True, "writer"),
        (False, "writer"),
    ]
    [write] = local.state.writes()
    assert (write["outcome"], write["approval_id"]) == ("executed", approval.approval_id)
    assert len(local.state.used_nonces()) == 1
    mitigations = [
        e for e in runtime.events.list_events(record.run_id) if e.category == C.MITIGATION
    ]
    assert len(mitigations) == 1
    # Both processes did their own cluster pinning, and every command names an identity.
    assert {c["identity"] for c in local.state.commands()} == {
        "reader",
        "writer",
        "oracle",
        "scenario",
    }
    # Nothing secret or local left the adapter: no capability, no credential path.
    written = everything_written(runtime, record)
    assert CAPABILITY.search(written) is None and '"capability": "ey' not in written
    assert str(credentials) not in written and ".kubeconfig" not in written
    assert str(credentials) not in json.dumps(local.state.commands())


async def test_a_planted_instruction_in_the_logs_changes_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    runtime, _ = kind_runtime(settings, tmp_path)

    record = await run_with_operator(runtime, "kind-injected-logs")

    assert record.state == IncidentState.RESOLVED, record.outcome_note
    assert [p.action for p in record.proposals] == ["execute_rollback"]
    assert all(d.tool_name != "delete_namespace" for d in record.safety_decisions)
    local = adapter(runtime, record.run_id)
    assert [w["action"] for w in local.state.writes()] == ["rollback"]


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (RunConfig(approval_mode="auto"), "auto-approver is allowed only in a simulated"),
        (RunConfig(safety_enabled=False), "safety can only be disabled against a simulated"),
        (RunConfig(provider="replay"), "a replay may run only against a simulated environment"),
        (RunConfig(replay_fixture="anything"), "a replay may run only against a simulated"),
    ],
    ids=["auto-approver", "safety-off", "replay-provider", "replay-fixture"],
)
async def test_unsafe_run_modes_are_refused_before_the_cluster_is_touched(
    settings: Settings, tmp_path: Path, config: RunConfig, reason: str
) -> None:
    runtime, _ = kind_runtime(settings, tmp_path)

    with pytest.raises(ConfigError, match=reason):
        await runtime.run_incident("kind-bad-release", config, run_id="run-refused01")

    sandbox = runtime.settings.paths.sandbox_dir
    # reset never ran: no run file, no lease, not even the fake cluster was seeded.
    assert not (sandbox / "kind-run-refused01.db").exists()
    assert not (sandbox / "kind-leases.db").exists()
    assert not (sandbox / "kind-fake-cluster.db").exists()


async def test_the_write_boundary_holds_inside_the_mcp_server_for_any_caller(
    settings: Settings, tmp_path: Path
) -> None:
    """A caller that speaks MCP but is not RECAP's gateway: reads work; a forged capability,
    delete_namespace and a second use of a valid capability are refused in the subprocess."""
    runtime, _ = kind_runtime(settings, tmp_path)
    config = runtime.settings.environment
    controller_side = KubernetesEnvironment("run-mcp00001", **config.options)
    await controller_side.reset("kind-bad-release", 7)
    rollback = {"service": "backend", "to_version": "v1.0.0"}
    signed = capability_args(controller_side, "execute_rollback", rollback, approval_id="apr-mcp")

    async with McpToolClient.stdio(config, "run-mcp00001") as client:
        assert len(await client.list_tools()) == 10
        health = await client.call_tool("get_service_health", {"service": "backend"}, 10.0)
        forged = await client.call_tool(
            "execute_rollback", {**rollback, "capability": "eyJmb3JnZWQ.c2ln"}, 10.0
        )
        delete = await client.call_tool("delete_namespace", {"namespace": "recap-demo"}, 10.0)
        first = await client.call_tool("execute_rollback", signed, 10.0)
        second = await client.call_tool("execute_rollback", signed, 10.0)
        unknown = await client.call_tool(
            "get_service_health", {"service": "--all-namespaces"}, 10.0
        )

    assert health.ok and HealthSnapshot.model_validate(health.structured).error_rate > 0.05
    outcomes = [ActionOutcome.model_validate(r.structured) for r in (forged, delete, first, second)]
    assert [o.ok for o in outcomes] == [False, False, True, False]
    assert "not authorized" in outcomes[0].message and "not supported" in outcomes[1].message
    assert outcomes[3].message == "not authorized: the capability was already used"
    assert not unknown.ok and "unknown service" in str(unknown.error)
    local = adapter(runtime, "run-mcp00001")
    assert len([c for c in local.state.commands() if c["op"] == "rollout_undo"]) == 2  # dry + real
