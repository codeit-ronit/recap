"""The whole pipeline on the Kind adapter, through the real MCP server subprocess, offline.

The kind-demo target profile with one change: `client: fake` (the offline fake cluster,
synthetic data; recap/environments/kubernetes/fake.py). Everything else is real: the
controller, the agents (scripted policy), the MCP server subprocess building its own
KubernetesEnvironment from the config, the gateway, the policy (policy-kind-v1), an
approval decided through the External channel as an operator would in the dashboard, the
signed capability, the adapter's write path and its oracle. Like
tests/e2e/test_environment_swap.py, it proves the wiring; what only a real cluster can show
(RBAC, the admission policy, real rollout times) is in tests/e2e/test_kind_live.py.

Two tests differ: one takes its decisions from a scripted stand-in for a model (the real
Ollama protocol to a fake server, no model), and one runs SREGym's external-harness mode (the
sregym-hotel profile) on that target's own offline fake.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import REPO_ROOT, EnvironmentConfig, Settings, load_settings
from recap.core.errors import ApprovalError, ConfigError
from recap.core.interfaces import LLMProvider
from recap.core.models import ApprovalStatus, IncidentState, RunConfig, RunRecord
from recap.core.observations import ActionOutcome, HealthSnapshot
from recap.environments.kubernetes import KubernetesEnvironment
from recap.providers.factory import create_provider
from recap.runtime import Runtime, stdio_transport
from recap.safety.approvals import ExternalApprovalChannel
from recap.telemetry.tracer import TraceCategory as C
from recap.tools.mcp_client import McpToolClient
from tests.helpers import capability_args
from tests.integration.test_controller_paths import wait_for_pending
from tests.llm_fakes import FakeOllama, ScriptedModel, hypothesis
from tests.unit.test_kind_engine import fake_data
from tests.unit.test_kind_external import DATA as SREGYM_DATA
from tests.unit.test_kind_external import GOOD, GOOD_IMAGE
from tests.unit.test_kind_external import SERVICES as SREGYM_SERVICES
from tests.unit.test_kind_preflight import write_credentials

pytestmark = pytest.mark.e2e  # every test here runs a real incident end to end

OPERATOR = "operator:kind-e2e"
CAPABILITY = re.compile(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")
RUN_LIMIT_SECONDS = 120.0
NAMESPACE = "recap-demo"
WRITE_OPS = ("rollout_undo", "rollout_restart")


def kind_runtime(settings: Settings, tmp_path: Path, **changes: Any) -> tuple[Runtime, Path]:
    """The kind-demo profile on the offline fake, with a real (synthetic) credentials folder
    so both processes run the cluster-pinning checks. `changes` replace adapter options."""
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
    options.update(changes)
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


# --- a slow rollout, judged by the Kind oracle ------------------------------------------------


def slow_rollouts_after_the_fault(runtime: Runtime, tmp_path: Path, seconds: float) -> None:
    """From the moment the agents start, a new ReplicaSet takes `seconds` to take over (the
    old pods serve until then, as with maxUnavailable 0). `runtime` reads its seed from
    fake_data(tmp_path). The fault itself went in at the fake's usual speed, so the alert
    fired as usual: the controller's fake read the seed during the reset, and the MCP server
    subprocess, which sends the fix, reads it only when it starts, right after the reset."""

    def transport(environment: EnvironmentConfig, run_id: str) -> McpToolClient:
        fake_data(tmp_path, rollout_seconds=seconds)
        return stdio_transport(environment, run_id)

    runtime.transport_factory = transport


def real_writes(local: KubernetesEnvironment) -> list[tuple[str, str]]:
    """(operation, identity) of every write that reached the cluster, dry runs left out."""
    return [
        (c["op"], c["identity"])
        for c in local.state.commands()
        if c["op"] in WRITE_OPS and "--dry-run=server" not in c["argv"]
    ]


@pytest.mark.parametrize(
    ("rollout_seconds", "window_seconds"),
    [(2.0, 20.0), (600.0, 1.0)],
    ids=["inside-the-window", "beyond-the-window"],
)
async def test_a_slow_rollout_is_judged_by_the_kind_oracle(
    settings: Settings, tmp_path: Path, rollout_seconds: float, window_seconds: float
) -> None:
    """The approved rollback starts a rollout that takes time; the first probe after the write
    sees it still running, so the controller keeps probing within the target's stabilization
    window. Inside the window the oracle sees the new pods serve: RESOLVED, one write. Beyond
    it, the verdict stands: not recovered, never RESOLVED. The controller then investigates
    again, and its next fix (a restart) is a new proposal with its own approval request,
    decided and used once: no write ever runs on an earlier approval. The lease is given back
    either way."""
    runtime, _ = kind_runtime(
        settings,
        tmp_path,
        fake_data_dir=str(fake_data(tmp_path)),  # a copy of the seed, at the usual speed
        stabilization_timeout_seconds=window_seconds,
    )
    slow_rollouts_after_the_fault(runtime, tmp_path, rollout_seconds)

    record = await run_with_operator(runtime, "kind-bad-release")

    events = runtime.events.list_events(record.run_id)
    verdicts = [e for e in events if e.category == C.VERIFY_RECOVERY]
    first = verdicts[0].payload
    assert first["probes"] > 1  # the rollout was still running at the first probe
    local = adapter(runtime, record.run_id)
    assert local.lease.holder(NAMESPACE) is None
    if rollout_seconds < window_seconds:
        assert record.state == IncidentState.RESOLVED, record.outcome_note
        assert record.recovery is not None and record.recovery.verified
        assert first["verified"] and first["waited_seconds"] < window_seconds
        assert [(a.action, a.result) for a in record.actions] == [("execute_rollback", "recovered")]
        assert real_writes(local) == [("rollout_undo", "writer")]
        return
    assert first["verified"] is False and first["waited_seconds"] >= window_seconds
    assert record.state == IncidentState.FAILED
    assert record.outcome_note == "mitigation did not recover the system; attempts used up"
    assert record.recovery is not None and not record.recovery.verified
    assert [(a.action, a.result) for a in record.actions] == [
        ("execute_rollback", "not_recovered"),
        ("restart_service", "not_recovered"),
    ]
    assert real_writes(local) == [("rollout_undo", "writer"), ("rollout_restart", "writer")]
    # Each write ran under its own approval, for its own proposal, used exactly once.
    rollback, restart = record.approvals
    assert rollback.approval_id != restart.approval_id
    assert rollback.proposal_digest != restart.proposal_digest
    assert rollback.consumed_at is not None and restart.consumed_at is not None
    assert [(w["action"], w["outcome"], w["approval_id"]) for w in local.state.writes()] == [
        ("rollback", "executed", rollback.approval_id),
        ("restart", "executed", restart.approval_id),
    ]
    # The second fix was asked for anew, only after the oracle's verdict on the first.
    requested = [e.seq for e in events if e.category == C.APPROVAL_REQUESTED]
    judged = [e.seq for e in verdicts]
    assert requested[0] < judged[0] < requested[1] < judged[1]


# --- cancellation while waiting for a person ------------------------------------------------


async def test_cancelling_a_kind_run_while_it_waits_writes_nothing_and_frees_the_namespace(
    settings: Settings, tmp_path: Path
) -> None:
    """Cancelled while the operator has not answered (POST /runs/{id}/cancel cancels the run's
    task the same way): the run ends FAILED "run cancelled", its request expires so nobody can
    approve it later, no write command reaches the cluster, and the namespace lease is given
    back at once: the next run resets and resolves without waiting for the lease to expire."""
    runtime, _ = kind_runtime(settings, tmp_path)
    run_id = "run-kindcancel1"
    task = asyncio.create_task(
        runtime.run_incident(
            "kind-bad-release",
            RunConfig(memory_write=False),
            approval_channel=ExternalApprovalChannel(),
            run_id=run_id,
        )
    )
    proposal_id = await wait_for_pending(runtime, task)
    local = adapter(runtime, run_id)
    holder = local.lease.holder(NAMESPACE)
    assert holder is not None and holder.run_id == run_id  # held while it waits

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = runtime.runs.get(run_id)
    assert record is not None and record.state == IncidentState.FAILED
    assert record.outcome_note == "run cancelled"
    assert record.approvals == [] and record.actions == [] and record.episode_id is None
    request = runtime.approvals.get_request(proposal_id)
    assert request is not None and request.status == ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalError, match="is expired, not pending"):
        runtime.approvals.decide(
            proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor=OPERATOR,
        )
    assert [c for c in local.state.commands() if c["op"] in WRITE_OPS] == []  # not even dry
    assert local.state.writes() == [] and local.state.used_nonces() == set()
    assert local.lease.holder(NAMESPACE) is None
    assert runtime.events.list_events(run_id)[-1].category == C.RUN_SUMMARY

    following = await run_with_operator(runtime, "kind-bad-release")

    assert following.state == IncidentState.RESOLVED, following.outcome_note


# --- an unready backend, restarted ------------------------------------------------------------


class MemoryReadingModel(ScriptedModel):
    """A scripted stand-in for a model (it does NOT reason): it reads backend's memory, events
    and logs, claims memory pressure on backend citing what it saw, and proposes a restart."""

    def __init__(self) -> None:
        super().__init__(service="backend", fixes=("restart_service",))
        self.plan = [
            [
                ("get_service_metrics", {"service": "backend", "metric": "memory_mb"}),
                ("get_k8s_like_events", {"service": "backend"}),
            ],
            [("get_service_logs", {"service": "backend"})],
        ]

    def _hypothesis(self, ids: list[str]) -> dict[str, Any]:
        return hypothesis(
            "backend",
            ids[:8],
            mechanism="resource_pressure",
            version=None,
            claim="backend's memory is at its limit, so its readiness refuses traffic",
        )


async def test_an_unready_backend_is_restarted_after_an_operators_approval(
    settings: Settings, tmp_path: Path
) -> None:
    """kind-report-leak: a burst of reports fills backend's memory, its readiness refuses
    traffic (0 of 2 replicas ready: no error rate can even be measured) and frontend fails.
    On this fake the scripted baseline never reads backend's memory and hands the run over to
    a person (FAILED, nothing written), so the decisions here come from a scripted stand-in
    for a model, through the real Ollama protocol to a fake server (no model runs). The
    verifier supports the memory-pressure claim from the memory metric at the limit, the
    operator approves the exact restart, the writer sends it once, and the oracle sees both
    backend replicas ready again."""
    runtime, _ = kind_runtime(settings, tmp_path)
    server = FakeOllama(responder=MemoryReadingModel())

    def make(config: RunConfig) -> LLMProvider:
        return create_provider(
            "ollama", runtime.settings, mock_http=server.mock(), ledger=runtime.ledger
        )

    runtime.make_provider = make  # type: ignore[method-assign]

    record = await run_with_operator(runtime, "kind-report-leak")

    assert record.state == IncidentState.RESOLVED, record.outcome_note
    assert record.provider_info is not None and record.provider_info.origin == "local_llm"
    assert record.verification is not None and record.verification.best is not None
    best = record.verification.best
    assert (best.mechanism.value, best.target_service) == ("resource_pressure", "backend")
    [proposal] = record.proposals
    assert (proposal.action, proposal.target, proposal.params) == (
        "restart_service",
        "backend",
        {},
    )
    [approval] = record.approvals
    assert approval.actor == OPERATOR and approval.consumed_at is not None
    assert [(a.action, a.result) for a in record.actions] == [("restart_service", "recovered")]
    assert record.recovery is not None and record.recovery.healthy_before is False
    before = record.recovery.observed_signals["before"]["backend"]
    after = record.recovery.observed_signals["after"]["backend"]
    assert before["ready_replicas"] == 0 and after["ready_replicas"] == after["replicas"] == 2
    local = adapter(runtime, record.run_id)
    restarts = [c for c in local.state.commands() if c["op"] == "rollout_restart"]
    assert [("--dry-run=server" in c["argv"], c["identity"]) for c in restarts] == [
        (True, "writer"),
        (False, "writer"),
    ]
    assert [(w["action"], w["approval_id"]) for w in local.state.writes()] == [
        ("restart", approval.approval_id)
    ]
    assert local.lease.holder(NAMESPACE) is None


# --- an external harness's problem (SREGym's mode), rolled back to its restore point ------------


def external_runtime(settings: Settings, tmp_path: Path) -> Runtime:
    """The sregym-hotel profile on the offline fake, with the synthetic problem of
    tests/fixtures/sregym: three services, and geo released by the harness with an image that
    crashes at start-up, before the run. A container must stay up 1 s (not a minute) before
    the oracle, which has no probe here, calls it settled."""
    del settings  # loaded for its environment variables (latency 0, operator token, ...)
    credentials = write_credentials(tmp_path / "private" / "sregym", context="kind-kind")
    sregym = load_settings(data_dir=tmp_path / "data", target="sregym-hotel")
    options = dict(sregym.environment.options)
    options.update(
        client="fake",
        fake_data_dir=str(SREGYM_DATA),
        scenarios_dir=str(SREGYM_DATA / "scenarios"),
        credentials_dir=str(credentials),
        services=SREGYM_SERVICES,
        image_repository="ghcr.io/example/hotel-reservation",
        dependencies={"frontend": ["geo"], "geo": ["mongodb-geo"]},
        probe_round_gap_seconds=0.0,
        alert_poll_seconds=0.02,
        stabilization_poll_seconds=0.2,
        stabilization_timeout_seconds=20.0,
        unprobed_min_running_seconds=1.0,
    )
    environment = sregym.environment.model_copy(update={"options": options})
    return Runtime(sregym.model_copy(update={"environment": environment}))


async def test_the_external_harness_problem_is_rolled_back_to_its_restore_point(
    settings: Settings, tmp_path: Path
) -> None:
    """SREGym's external-harness mode, offline: the harness deployed the app and released a
    crashing geo image before the run. RECAP's reset changes nothing (it reads as recap-reader
    and recap-oracle; recap-scenario is never used). The agents find the release; the proposal
    rolls geo back to the tag of the revision the harness first deployed; the operator
    approves it; the writer restores exactly that revision's image, digest included. Without a
    probe the oracle calls geo recovered only once its new container has stayed up."""
    runtime = external_runtime(settings, tmp_path)
    info = runtime.environment_info()
    assert (info.kind, info.is_simulated) == ("sregym", False)

    record = await run_with_operator(runtime, "test-external-geo")

    assert record.state == IncidentState.RESOLVED, record.outcome_note
    assert record.outcome_note == "oracle verified recovery (sregym)"
    [proposal] = record.proposals
    assert (proposal.action, proposal.target, proposal.params) == (
        "execute_rollback",
        "geo",
        {"to_version": GOOD},
    )
    [approval] = record.approvals
    assert approval.actor == OPERATOR and approval.consumed_at is not None
    local = adapter(runtime, record.run_id)
    commands = local.state.commands()
    assert {c["identity"] for c in commands} == {"reader", "writer", "oracle"}
    changes = [c for c in commands if c["op"] in (*WRITE_OPS, "set_image")]
    assert [(c["op"], "--dry-run=server" in c["argv"]) for c in changes] == [
        ("rollout_undo", True),
        ("rollout_undo", False),
    ]
    [write] = local.state.writes()
    assert (write["outcome"], write["approval_id"]) == ("executed", approval.approval_id)
    assert (write["detail"]["image_after"], write["detail"]["to_revision"]) == (GOOD_IMAGE, 1)
    [verdict] = [
        e.payload
        for e in runtime.events.list_events(record.run_id)
        if e.category == C.VERIFY_RECOVERY
    ]
    assert verdict["verified"] and verdict["probes"] > 1  # not before the container stayed up
    assert local.lease.holder("hotel-reservation") is None
