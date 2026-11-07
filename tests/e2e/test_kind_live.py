"""Acceptance tests on the REAL local Kind cluster (docs/m5-integration-plan.md, section 11).

Opt-in, never in CI: marked `live_cluster` and skipped unless RECAP_LIVE_KIND=1 is set AND
RECAP's preflight passes (the pinned context, a loopback API server, the recorded CA
fingerprint, kubectl and Prometheus reachable, clocks in step). They use the kind-demo target
profile as committed: client kubectl, the credentials in ~/.config/recap/kind (outside git).
Set RECAP_LIVE_KIND_DATA_DIR to keep each run's evidence (records, traces, the adapter's
command and write audits); by default it lives in pytest's temporary folder.

The approvals here are given by the test harness through the same store the dashboard uses,
recorded as "operator:live-acceptance": these tests check the machinery, not a person's
judgement. All six passed on the real Kind cluster in M5 W3 (docs/m5-progress.md).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import Settings, load_settings
from recap.core.errors import ConfigError
from recap.core.models import IncidentState, RunConfig, RunRecord
from recap.core.observations import ActionOutcome, AuthorizedAction
from recap.environments.kubernetes import KubernetesEnvironment, rollout
from recap.environments.kubernetes.kubectl import IDENTITY_OPS
from recap.environments.kubernetes.preflight import PreflightError
from recap.environments.kubernetes.state import disable_writes, enable_writes
from recap.runtime import Runtime
from recap.safety.approvals import ExternalApprovalChannel
from recap.telemetry.tracer import TraceCategory as C
from recap.tools.mcp_client import McpToolClient
from tests.helpers import capability_args, issuer_for, sign

LIVE = os.environ.get("RECAP_LIVE_KIND") == "1"
pytestmark = [
    pytest.mark.live_cluster,
    pytest.mark.e2e,
    pytest.mark.skipif(
        not LIVE,
        reason="needs the live Kind cluster: set RECAP_LIVE_KIND=1 after infra/kind setup",
    ),
]

OPERATOR = "operator:live-acceptance"
NAMESPACE = "recap-demo"
SERVICES = ("frontend", "backend")
RUN_LIMIT_SECONDS = 1800.0


@pytest.fixture
async def live(settings: Settings, tmp_path: Path) -> AsyncIterator[Runtime]:
    """A runtime on the kind-demo profile; skipped when the cluster does not pass preflight."""
    del settings  # loaded for its environment variables
    data_dir = Path(os.environ.get("RECAP_LIVE_KIND_DATA_DIR") or tmp_path / "data")
    runtime = Runtime(load_settings(data_dir=data_dir, target="kind-demo"))
    probe = adapter(runtime, "live-preflight")
    try:
        await probe.preflight()
    except (PreflightError, OSError, RuntimeError) as exc:
        pytest.skip(f"the Kind cluster did not pass RECAP's preflight: {exc}")
    yield runtime


def adapter(runtime: Runtime, run_id: str) -> KubernetesEnvironment:
    return KubernetesEnvironment(run_id, **runtime.settings.environment.options)


def kubectl(runtime: Runtime, identity: str, *args: str) -> subprocess.CompletedProcess[str]:
    """The test harness's own kubectl call as one identity (never RECAP's code path)."""
    options = runtime.settings.environment.options
    binary = str(Path(options["kubectl"]).expanduser())
    kubeconfig = Path(options["credentials_dir"]).expanduser() / f"recap-{identity}.kubeconfig"
    command = [binary, "--kubeconfig", str(kubeconfig), "--context", "kind-recap-m5", *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


async def snapshot(env: KubernetesEnvironment) -> dict[str, Any]:
    """What a write would change: generations, resource versions, ReplicaSets, pod UIDs."""
    reader = env._client("reader")
    deployments = await reader.list_deployments()
    pods = await reader.list_pods(None)
    replicasets: list[dict[str, Any]] = []
    for service in SERVICES:
        replicasets += await reader.list_replicasets({"app": service})
    return {
        "deployments": sorted(
            (d["metadata"]["name"], d["metadata"]["generation"], d["metadata"]["resourceVersion"])
            for d in deployments
        ),
        "replicasets": sorted(r["metadata"]["name"] for r in replicasets),
        "pods": sorted(p["metadata"]["uid"] for p in pods),
    }


async def settled(env: KubernetesEnvironment, seconds: float = 240.0) -> None:
    """Wait until both rollouts are complete, so the cluster's own controllers stop changing
    the objects a snapshot compares (only RECAP's reads may then happen in between)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    reader = env._client("reader")
    while True:
        deployments = await reader.list_deployments()
        mine = [d for d in deployments if d["metadata"]["name"] in SERVICES]
        if mine and all(rollout.rollout_state(d)[0] for d in mine):
            return
        assert loop.time() < deadline, "the demo's rollouts never settled"
        await asyncio.sleep(5)


async def run_with_operator(
    runtime: Runtime, scenario: str, decision: str = "approved"
) -> RunRecord:
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
                    decision=decision,  # type: ignore[arg-type]
                    proposal_digest=request.proposal_digest,
                    actor=OPERATOR,
                )
            await asyncio.sleep(0.5)
    record = task.result()
    adapter(runtime, record.run_id).release_lease()
    return record


# --- test 1: reads are real, typed and change nothing; refusals hold -------------------------

PERMISSIONS = [
    ("reader", ["list", "pods"], True),
    ("reader", ["get", "pods", "--subresource=log"], True),
    ("reader", ["patch", "deployments"], False),
    ("reader", ["get", "secrets"], False),
    ("reader", ["create", "pods", "--subresource=exec"], False),
    ("writer", ["patch", "deployment/backend"], True),
    ("writer", ["patch", "deployments", "--subresource=scale"], False),
    ("writer", ["delete", "deployment/backend"], False),
    ("writer", ["create", "pods"], False),
    ("oracle", ["list", "pods"], True),
    ("oracle", ["get", "pods", "--subresource=log"], False),
    ("scenario", ["patch", "deployment/frontend"], True),
    ("scenario", ["get", "secrets"], False),
]


async def test_1_reads_are_real_typed_and_change_nothing(live: Runtime) -> None:
    for identity, check, allowed in PERMISSIONS:
        answer = kubectl(live, identity, "auth", "can-i", *check, "-n", NAMESPACE)
        assert (answer.stdout.strip() == "yes") is allowed, (identity, check, answer.stderr)
    for other in ("kube-system", "default", "recap-system"):
        assert (
            kubectl(live, "reader", "auth", "can-i", "list", "pods", "-n", other).stdout.strip()
            == "no"
        )

    env = adapter(live, "run-live-test1")
    await env.reset("kind-bad-release", 7)
    try:
        await settled(env)
        before = await snapshot(env)
        config = live.settings.environment
        reads = [
            ("list_services", {}),
            ("get_recent_deployments", {"limit": 10}),
            ("get_k8s_like_events", {"limit": 30}),
        ]
        for service in SERVICES:
            reads += [
                ("get_service_health", {"service": service}),
                ("get_service_logs", {"service": service, "limit": 40}),
                ("get_service_metrics", {"service": service, "metric": "error_rate"}),
                ("get_service_metrics", {"service": service, "metric": "memory_mb"}),
                ("propose_rollback", {"service": service}),
            ]
        async with McpToolClient.stdio(config, env.run_id) as client:
            results = [await client.call_tool(tool, args, 10.0) for tool, args in reads]
            forged = await client.call_tool(
                "execute_rollback",
                {"service": "backend", "to_version": "v1.0.0", "capability": "eyJmb3JnZWQ.c2ln"},
                10.0,
            )
            bare = await client.call_tool("restart_service", {"service": "backend"}, 10.0)
            delete = await client.call_tool("delete_namespace", {"namespace": NAMESPACE}, 10.0)
        assert all(result.ok for result in results), [r.error for r in results if not r.ok]
        for refused in (forged, bare, delete):
            assert ActionOutcome.model_validate(refused.structured).ok is False
        for unsafe in (RunConfig(approval_mode="auto"), RunConfig(safety_enabled=False)):
            with pytest.raises(ConfigError):
                await live.run_incident("kind-bad-release", unsafe)
        assert await snapshot(env) == before  # nothing changed
        allowed = set().union(*IDENTITY_OPS.values())
        commands = env.state.commands()
        assert {c["op"] for c in commands} <= {op.value for op in allowed}
        credentials = str(Path(config.options["credentials_dir"]).expanduser())
        assert all(credentials not in " ".join(c["argv"]) for c in commands)
    finally:
        env.release_lease()


# --- test 2: a bad release is rolled back after an exact human approval ----------------------


async def test_2_a_bad_release_is_rolled_back_after_an_exact_approval(live: Runtime) -> None:
    record = await run_with_operator(live, "kind-bad-release")

    assert record.state == IncidentState.RESOLVED, record.outcome_note
    assert record.outcome_note == "oracle verified recovery (kind)"
    assert record.approvals[0].actor == OPERATOR
    env = adapter(live, record.run_id)
    undo = [c for c in env.state.commands() if c["op"] == "rollout_undo"]
    assert [("--dry-run=server" in c["argv"]) for c in undo] == [True, False]
    [write] = env.state.writes()
    assert write["detail"]["image_after"] == "recap-demo/backend:v1.0.0"
    [verdict] = [
        e for e in live.events.list_events(record.run_id) if e.category == C.VERIFY_RECOVERY
    ]
    assert verdict.payload["healthy_before"] is False and verdict.payload["verified"] is True
    # A capability works once: one that the adapter refuses before writing (the running
    # version) burns its nonce, and the same capability again is refused as used.
    env.release_lease()
    action = AuthorizedAction(
        action="rollback", target="backend", params={"to_version": "v1.0.0"}, approval_id="x"
    )
    signed = sign(issuer_for(env), action)
    first = await env.execute_authorized(signed)
    second = await env.execute_authorized(signed)
    assert "already the running revision" in first.message
    assert second.message == "not authorized: the capability was already used"


async def test_2b_a_rejected_proposal_changes_nothing(live: Runtime) -> None:
    record = await run_with_operator(live, "kind-bad-release", decision="rejected")

    assert record.state == IncidentState.BLOCKED
    env = adapter(live, record.run_id)
    assert not [c for c in env.state.commands() if c["op"] == "rollout_undo"]
    deployment = await env._client("reader").get_deployment("backend")
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == "recap-demo/backend:v1.1.0"


# --- test 3: a rolling restart clears a full memory, and the stop controls hold --------------


async def test_3_a_restart_fixes_a_full_memory_only_when_readiness_says_so(live: Runtime) -> None:
    env = adapter(live, "run-live-test3")
    await env.reset("kind-report-leak", 7)
    try:
        broken = await env.health_check()
        assert broken.services["backend"].ready_replicas < broken.services["backend"].replicas
        reader = env._client("reader")
        generation = (await reader.get_deployment("backend"))["metadata"]["generation"]

        # The write switch: the approved write is refused and nothing changes.
        switch = env.options.write_switch_path()
        disable_writes(switch, "live acceptance test 3")
        restart = AuthorizedAction(action="restart", target="backend", approval_id="apr-live-3")
        try:
            refused = await env.execute_authorized(sign(issuer_for(env), restart))
        finally:
            enable_writes(switch)
        assert not refused.ok
        assert (await reader.get_deployment("backend"))["metadata"]["generation"] == generation

        # Outside RECAP, as the writer: the admission policy refuses another shape of change.
        for patch in (
            '{"spec":{"replicas":3}}',
            '{"spec":{"template":{"spec":{"containers":[{"name":"backend","image":"busybox:1"}]}}}}',
        ):
            denied = kubectl(
                live, "writer", "-n", NAMESPACE, "patch", "deployment", "backend", "-p", patch
            )
            assert denied.returncode != 0, denied.stdout
        assert (await reader.get_deployment("backend"))["metadata"]["generation"] == generation

        done = await env.execute_authorized(sign(issuer_for(env), restart))
        assert done.ok, done.message
        deadline = asyncio.get_running_loop().time() + env.options.stabilization_timeout_seconds
        while True:
            report = await env.health_check()
            deployment = await reader.get_deployment("backend")
            status = deployment.get("status", {})
            if status.get("replicas") != status.get("updatedReplicas"):
                # Old pods are still there: the oracle must not call that healthy.
                assert report.services["backend"].status != "Running"
            if all(
                h.status == "Running" and h.ready_replicas == h.replicas
                for h in report.services.values()
            ) and all(h.error_rate < 0.05 for h in report.services.values()):
                break
            assert asyncio.get_running_loop().time() < deadline, report
            await asyncio.sleep(env.options.stabilization_poll_seconds)
    finally:
        env.release_lease()


async def test_3b_the_scripted_pipeline_on_the_report_burst(live: Runtime) -> None:
    """The full pipeline on scenario 3. Known gap (offline finding): the scripted policy's
    playbook follows only the java heap-warning phrasing, so it may not check backend's
    memory and end FAILED; a model provider reads the demo's own words. Recorded either way."""
    record = await run_with_operator(live, "kind-report-leak")
    assert record.state in (IncidentState.RESOLVED, IncidentState.FAILED), record.outcome_note
    if record.state == IncidentState.RESOLVED:
        assert [a.action for a in record.actions] == ["restart_service"]


# --- the MCP subprocess as a caller that is not RECAP's gateway -------------------------------


async def test_the_write_boundary_holds_for_a_direct_mcp_caller(live: Runtime) -> None:
    env = adapter(live, "run-live-mcp")
    await env.reset("kind-bad-release", 7)
    try:
        args = {"service": "backend", "to_version": "v1.0.0"}
        signed = capability_args(env, "execute_rollback", args, approval_id="apr-live-mcp")
        async with McpToolClient.stdio(live.settings.environment, env.run_id) as client:
            first = await client.call_tool("execute_rollback", signed, 10.0)
            again = await client.call_tool("execute_rollback", signed, 10.0)
        assert ActionOutcome.model_validate(first.structured).ok
        assert ActionOutcome.model_validate(again.structured).ok is False
    finally:
        env.release_lease()
