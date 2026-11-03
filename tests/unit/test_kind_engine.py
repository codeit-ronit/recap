"""KubernetesEnvironment, the Kind adapter, over the offline fake cluster (client: fake).

The adapter's own code runs unchanged: options, preflight, the lease, typed operations,
strict parsing, translation, the write path and the oracle. Only the cluster is a fake
(recap/environments/kubernetes/fake.py, seeded from tests/fixtures/kind/cluster.json:
synthetic). What the real cluster adds (RBAC, the admission policy, real timing) is tested
by tests/e2e/test_kind_live.py, which runs only against the live cluster.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.ids import utcnow
from recap.core.observations import AuthorizedAction
from recap.environments.kubernetes import KubernetesEnvironment
from recap.environments.kubernetes.kubectl import KubeError, KubeOp
from recap.environments.kubernetes.metrics import MetricsUnavailable
from recap.environments.kubernetes.preflight import PreflightError
from recap.environments.kubernetes.state import NOT_RESET, LeaseHeld, disable_writes, enable_writes
from recap.safety.capabilities import CapabilityIssuer
from tests.helpers import issuer_for, sign
from tests.unit.test_kind_preflight import write_credentials

FAKE_DATA = REPO_ROOT / "tests" / "fixtures" / "kind"
SCENARIOS = REPO_ROOT / "fixtures" / "kind-scenarios"
RUN = "run-kind00001"
ROLLBACK = AuthorizedAction(
    action="rollback",
    target="backend",
    params={"to_version": "v1.0.0"},
    approval_id="apr-kind-1",
    proposal_digest="d" * 64,
)
RESTART = AuthorizedAction(
    action="restart", target="backend", approval_id="apr-kind-2", proposal_digest="e" * 64
)
WRITE_OPS = ("rollout_undo", "rollout_restart")


def kind_options(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    """Options as load_settings would pass them, with the fake client and fast polling."""
    return {
        "scenarios_dir": str(SCENARIOS),
        "state_dir": str(tmp_path / "sandbox"),
        "client": "fake",
        "fake_data_dir": str(FAKE_DATA),
        "probe_round_gap_seconds": 0.0,
        "alert_poll_seconds": 0.01,
        "stabilization_poll_seconds": 0.1,
        **changes,
    }


def environment(tmp_path: Path, run_id: str = RUN, **changes: Any) -> KubernetesEnvironment:
    return KubernetesEnvironment(run_id, **kind_options(tmp_path, **changes))


async def ready(
    tmp_path: Path, scenario: str = "kind-bad-release", **changes: Any
) -> KubernetesEnvironment:
    env = environment(tmp_path, **changes)
    await env.reset(scenario, 7)
    return env


def fake_data(tmp_path: Path, **changes: Any) -> Path:
    """A copy of the fake's seed folder with some top-level values changed."""
    folder = tmp_path / "fake-data"
    folder.mkdir(exist_ok=True)
    spec = json.loads((FAKE_DATA / "cluster.json").read_text())
    spec.update(changes)
    (folder / "cluster.json").write_text(json.dumps(spec))
    return folder


def sent(env: KubernetesEnvironment) -> list[dict[str, Any]]:
    """Write commands that reached the (fake) cluster, dry runs included."""
    return [c for c in env.state.commands() if c["op"] in WRITE_OPS]


def real_writes(env: KubernetesEnvironment) -> list[dict[str, Any]]:
    return [c for c in sent(env) if "--dry-run=server" not in c["argv"] and c["exit_code"] == 0]


# --- built for every GET /system: no I/O, no secrets ------------------------------------------


def test_constructing_describing_and_listing_touch_nothing(tmp_path: Path) -> None:
    env = environment(tmp_path, run_id="describe", credentials_dir=str(tmp_path / "creds"))

    info = env.describe()
    scenarios = env.list_scenarios()

    assert not (tmp_path / "sandbox").exists() and not (tmp_path / "creds").exists()
    assert (info.kind, info.namespace, info.is_simulated) == ("kind", "recap-demo", False)
    assert info.supported_actions == ["rollback", "restart"]
    assert "not production" in info.description and "OFFLINE FAKE" in info.description
    assert info.name == "Kind cluster recap-m5 (offline fake)"
    shown = info.model_dump_json()
    for secretish in (str(tmp_path), "127.0.0.1", "http", "kubeconfig", "/"):
        assert secretish not in shown.replace("n/a", "")
    assert [s.id for s in scenarios] == [
        "kind-bad-release",
        "kind-dependency-crash",
        "kind-injected-logs",
        "kind-report-leak",
    ]


def test_the_real_client_is_described_as_a_local_cluster(tmp_path: Path) -> None:
    env = KubernetesEnvironment(
        "describe",
        **kind_options(tmp_path, client="kubectl", kubectl="/opt/homebrew/bin/kubectl"),
    )
    info = env.describe()
    assert info.name == "Kind cluster recap-m5" and "OFFLINE" not in info.description
    assert info.stabilization_timeout_seconds == 180 and info.stabilization_poll_seconds == 0.1


def test_the_scenarios_carry_evaluation_labels_only_in_the_catalog(tmp_path: Path) -> None:
    catalog = {s.id: s for s in environment(tmp_path).list_scenarios()}

    bad = catalog["kind-bad-release"]
    assert (bad.family, bad.partition.value, bad.alert_service) == (
        "bad_deployment",
        "demo",
        "frontend",
    )
    assert bad.expected["fix_params"] == {"to_version": "v1.0.0"}
    assert all(s.budget == {"max_elapsed_seconds": 1800} for s in catalog.values())
    assert catalog["kind-report-leak"].expected["fix"] == "restart_service"
    assert catalog["kind-injected-logs"].expected["redteam"] is True
    assert catalog["kind-dependency-crash"].alert_service == "backend"


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"surprise": 1}, "surprise"),
        ({"prometheus_url": "http://10.0.0.5:30090"}, "prometheus_url"),
        ({"prometheus_url": "http://user:pw@127.0.0.1:30090"}, "prometheus_url"),
        (
            {"service_urls": {"frontend": "http://example.com", "backend": "http://127.0.0.1:1"}},
            "service_urls",
        ),
        ({"service_urls": {"frontend": "http://127.0.0.1:1"}}, "service_urls"),
        ({"client": "kubectl", "kubectl": "kubectl"}, "kubectl"),
        ({"client": "kubectl"}, "kubectl"),
        ({"client": "fake", "fake_data_dir": None}, "fake_data_dir"),
        ({"namespace": "Recap_Demo"}, "namespace"),
        ({"services": ["frontend", "frontend"]}, "services"),
        ({"metrics_rate_window": "1m) or vector(1"}, "metrics_rate_window"),
        ({"request_timeout_seconds": float("nan")}, "request_timeout_seconds"),
        ({"stabilization_timeout_seconds": float("inf")}, "stabilization_timeout_seconds"),
        ({"write_deadline_seconds": 30}, "write_deadline_seconds"),
    ],
    ids=lambda value: str(value)[:30],
)
def test_bad_options_fail_at_start_naming_the_key_never_the_value(
    tmp_path: Path, changes: dict[str, Any], key: str
) -> None:
    with pytest.raises(ConfigError, match="invalid Kind adapter options") as refused:
        environment(tmp_path, **changes)
    message = str(refused.value)
    assert "10.0.0.5" not in message and "pw@" not in message and "example.com" not in message


def test_the_administrators_credential_is_never_accepted(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="administrator"):
        environment(tmp_path, kubeconfigs={"admin": str(tmp_path / "admin.kubeconfig")})


@pytest.mark.parametrize("run_id", ["../x", "a/b", "", ".hidden"])
def test_the_run_id_never_becomes_a_path_outside_the_state_folder(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ValueError, match="invalid run_id"):
        environment(tmp_path, run_id=run_id)


# --- reset: baseline, calm, fault, measured alert ----------------------------------------------


async def test_reset_returns_a_measured_alert_without_any_label(tmp_path: Path) -> None:
    env = environment(tmp_path)
    before = utcnow()

    alert = await env.reset("kind-bad-release", 7)

    assert alert.service == "frontend" and alert.affected_services == ["frontend", "backend"]
    assert alert.namespace == "recap-demo" and alert.fired_at >= before
    assert alert.metadata == {"source": "recap-kind-probe", "namespace": "recap-demo"}
    assert alert.symptoms[0] == (
        "frontend: HTTP error rate 0.40 over 10 probe requests (first failure: HTTP 500)"
    )
    text = alert.model_dump_json()
    for label in ("kind-bad-release", "bad_deployment", "v1.1.0", "v1.0.0", "rollback", "KeyError"):
        assert label not in text
    info = env.state.info()
    assert {s: (b.version, b.revision) for s, b in info.baselines.items()} == {
        "frontend": ("v1.0.0", 1),
        "backend": ("v1.0.0", 2),
    }
    assert info.observation_start == info.baseline_at - timedelta(seconds=120)
    [fault] = env.state.release_log()
    assert (fault.service, fault.version, fault.previous_version, fault.kind) == (
        "backend",
        "v1.1.0",
        "v1.0.0",
        "release",
    )
    assert env.lease.holder("recap-demo").run_id == RUN  # type: ignore[union-attr]
    # The fault went in as recap-scenario: a dry run, then the typed patch.
    patches = [c for c in env.state.commands() if c["op"] == "set_image"]
    assert [("--dry-run=server" in c["argv"], c["identity"]) for c in patches] == [
        (True, "scenario"),
        (False, "scenario"),
    ]


async def test_a_second_run_cannot_reset_the_namespace_while_the_first_holds_it(
    tmp_path: Path,
) -> None:
    await ready(tmp_path)
    second = environment(tmp_path, run_id="run-kind00002")

    with pytest.raises(LeaseHeld, match=f"leased by run {RUN}"):
        await second.reset("kind-bad-release", 7)
    assert not [c for c in second.state.commands() if c["op"] == "set_image"]


async def test_a_reset_that_fails_gives_the_namespace_back(tmp_path: Path) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    spec = yaml.safe_load((SCENARIOS / "kind-bad-release.yaml").read_text())
    spec["faults"] = [{"type": "set_image", "service": "backend", "version": "v0.9.0"}]  # healthy
    spec["alert"]["timeout_seconds"] = 0.2
    (scenarios / "kind-bad-release.yaml").write_text(yaml.safe_dump(spec))
    env = environment(tmp_path, scenarios_dir=str(scenarios))

    with pytest.raises(RuntimeError, match="did not fire"):
        await env.reset("kind-bad-release", 7)

    assert env.lease.holder("recap-demo") is None
    with pytest.raises(RuntimeError, match=NOT_RESET):
        await env.observe_services()


async def test_a_reset_restores_the_baseline_first(tmp_path: Path) -> None:
    env = await ready(tmp_path)  # backend now runs v1.1.0
    env.release_lease()
    again = environment(tmp_path, run_id="run-kind00002")

    await again.reset("kind-bad-release", 7)

    # backend went back to v1.0.0 before the fault was applied again.
    images = [c["argv"][-4] for c in again.state.commands() if c["op"] == "set_image"]
    assert any("v1.0.0" in image for image in images) and any("v1.1.0" in i for i in images)
    [fault] = again.state.release_log()
    assert fault.previous_version == "v1.0.0"
    # The re-released v1.1.0 is dated by THIS run's record, though Kubernetes reused its
    # ReplicaSet from the first run (the audit's gotcha).
    [release] = (await again.observe_deployments("backend", 10)).deployments
    assert release.deployed_at == fault.at and release.status == "complete"


async def test_preflight_pins_the_cluster_before_anything_runs(tmp_path: Path) -> None:
    credentials = write_credentials(tmp_path / "creds")
    env = environment(tmp_path, credentials_dir=str(credentials))
    await env.reset("kind-bad-release", 7)  # the pinned cluster: fine
    env.release_lease()

    other = write_credentials(tmp_path / "other")
    (credentials / "ca.sha256").write_text((other / "ca.sha256").read_text())
    stranger = environment(tmp_path, run_id="run-kind00002", credentials_dir=str(credentials))
    with pytest.raises(PreflightError, match="recorded fingerprint"):
        await stranger.reset("kind-bad-release", 7)
    assert stranger.lease.holder("recap-demo") is None  # stopped before the lease
    assert not [c for c in stranger.state.commands() if c["op"] == "set_image"]


async def test_a_clock_or_version_skew_stops_the_reset(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env.fake_cluster().clock = lambda: utcnow() + timedelta(seconds=30)
    with pytest.raises(PreflightError, match="clock is 30"):
        await env.reset("kind-bad-release", 7)

    old = fake_data(tmp_path, kubernetes={"client": "v1.37.1", "server": "v1.34.2"})
    skewed = environment(tmp_path, run_id="run-kind00002", fake_data_dir=str(old))
    with pytest.raises(PreflightError, match="more than one minor"):
        await skewed.reset("kind-bad-release", 7)


# --- reads --------------------------------------------------------------------------------------


async def test_reads_before_a_reset_are_refused_and_create_nothing(tmp_path: Path) -> None:
    env = environment(tmp_path)
    for read in (
        env.observe_services(),
        env.observe_health("backend"),
        env.observe_logs("backend", 10, 10),
        env.observe_metrics("backend", "error_rate", 60),
        env.observe_deployments(None, 10),
        env.observe_events(None, 10),
        env.plan_rollback("backend"),
    ):
        with pytest.raises(RuntimeError, match=NOT_RESET):
            await read
    with pytest.raises(RuntimeError, match=NOT_RESET):
        env.bind_executor("key")
    assert not (tmp_path / "sandbox").exists()


async def test_the_agents_view_of_a_bad_release(tmp_path: Path) -> None:
    env = await ready(tmp_path)

    services = {s.name: s for s in (await env.observe_services()).services}
    frontend = await env.observe_health("frontend")
    backend = await env.observe_health("backend")
    deployments = (await env.observe_deployments(None, 10)).deployments
    plan = await env.plan_rollback("backend")

    assert services["backend"].version == "v1.1.0" and services["frontend"].dependencies == [
        "backend"
    ]
    assert (frontend.status, frontend.ready_replicas, frontend.replicas) == ("Running", 2, 2)
    assert frontend.error_rate == pytest.approx(0.4) and backend.error_rate == pytest.approx(0.4)
    # Only this run's release; frontend's and backend's older history belong to other runs.
    assert [(d.service, d.version, d.previous_version) for d in deployments] == [
        ("backend", "v1.1.0", "v1.0.0")
    ]
    assert (plan.possible, plan.current_version, plan.target_version) == (True, "v1.1.0", "v1.0.0")
    assert "revision 2 (v1.0.0)" in plan.reason


async def test_logs_are_oldest_first_bounded_and_in_the_apps_phrasing(tmp_path: Path) -> None:
    env = await ready(tmp_path)

    frontend = await env.observe_logs("frontend", 8, 60)
    backend = await env.observe_logs("backend", 100, 60)

    stamps = [line.timestamp for line in frontend.lines]
    assert stamps == sorted(stamps) and len(frontend.lines) == 8 and frontend.truncated
    assert frontend.total_matched > 8
    assert any(line.message.startswith("rpc to backend failed") for line in frontend.lines)
    errors = [line for line in backend.lines if line.level == "ERROR"]
    assert errors and all("[build v1.1.0]" in line.message for line in errors)
    # Nothing older than this run's window (its baseline, minus the margin).
    start = env.state.info().observation_start
    assert all(line.timestamp >= start for line in frontend.lines + backend.lines)


async def test_metrics_carry_units_limits_and_a_calm_point_before_the_onset(tmp_path: Path) -> None:
    env = await ready(tmp_path)

    errors = await env.observe_metrics("backend", "error_rate", 60)
    memory = await env.observe_metrics("backend", "memory_mb", 60)
    cpu = await env.observe_metrics("backend", "cpu_pct", 60)
    restarts = await env.observe_metrics("backend", "restarts", 60)

    values = [p.value for p in errors.points]
    assert values[0] == 0.0 and values[-1] == pytest.approx(0.4)  # the onset is visible
    assert errors.unit == "ratio" and errors.step_minutes == 1
    assert memory.limit == 128.0 and "process RSS" in memory.unit
    assert cpu.points[-1].value == pytest.approx(100 * 0.012 / 0.25)
    assert [p.value for p in restarts.points] == [0.0] and restarts.limit is None


async def test_events_are_newest_first_and_only_from_this_runs_window(tmp_path: Path) -> None:
    env = await ready(tmp_path)

    events = (await env.observe_events(None, 30)).events
    backend = (await env.observe_events("backend", 30)).events
    frontend = (await env.observe_events("frontend", 30)).events

    assert [e.reason for e in events] == ["ScalingReplicaSet"]
    assert backend == events and frontend == []
    assert events[0].object == "deployment/backend"


@pytest.mark.parametrize(
    "service", ["payments", "-l app=backend", "--all-namespaces", "backend; rm -rf ~", "Backend", 7]
)
async def test_every_read_refuses_a_service_outside_the_allowlist(
    tmp_path: Path, service: Any
) -> None:
    env = await ready(tmp_path)
    commands = len(env.state.commands())

    for read in (
        env.observe_health(service),
        env.observe_logs(service, 10, 10),
        env.observe_metrics(service, "error_rate", 60),
        env.observe_deployments(service, 10),
        env.observe_events(service, 10),
        env.plan_rollback(service),
    ):
        with pytest.raises(ValueError, match="unknown service"):
            await read
    assert len(env.state.commands()) == commands  # refused before any kubectl call


async def test_numbers_from_a_direct_caller_are_clamped_or_refused(tmp_path: Path) -> None:
    env = await ready(tmp_path)

    many = await env.observe_logs("frontend", 10_000, 100_000)
    none = await env.observe_deployments(None, -5)
    short = await env.observe_metrics("backend", "error_rate", 1)

    assert len(many.lines) <= 100 and none.deployments == []
    assert short.points  # the window was raised to the 5-minute minimum
    with pytest.raises(ValueError, match="must be an integer"):
        await env.observe_logs("frontend", True, 10)
    with pytest.raises(ValueError, match="unknown metric"):
        await env.observe_metrics("backend", "disk_mb", 60)  # type: ignore[arg-type]


# --- the single write path ----------------------------------------------------------------------


async def test_a_valid_capability_gives_exactly_one_write_with_a_dry_run_first(
    tmp_path: Path,
) -> None:
    env = await ready(tmp_path)
    issuer = issuer_for(env)

    outcome = await env.execute_authorized(sign(issuer, ROLLBACK))

    assert outcome.ok, outcome.message
    assert outcome.message == "backend: rollout to revision 2 (v1.0.0) started"
    assert [("--dry-run=server" in c["argv"], c["identity"]) for c in sent(env)] == [
        (True, "writer"),
        (False, "writer"),
    ]
    [write] = real_writes(env)
    assert write["argv"][-3:] == ["--to-revision=2", "-o", "json"]
    [audit] = env.state.writes()
    assert (audit["outcome"], audit["approval_id"], audit["proposal_digest"]) == (
        "executed",
        "apr-kind-1",
        "d" * 64,
    )
    assert audit["detail"]["image_after"] == "recap-demo/backend:v1.0.0"
    assert audit["detail"]["generation_after"] == audit["detail"]["generation_before"] + 1
    # The run's own record now shows the rollback as the newest deployment.
    newest = (await env.observe_deployments("backend", 10)).deployments[0]
    assert (newest.version, newest.status, newest.previous_version) == (
        "v1.0.0",
        "rollback",
        "v1.1.0",
    )


async def test_a_capability_works_once(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    action = sign(issuer_for(env), ROLLBACK)
    assert (await env.execute_authorized(action)).ok

    again = await env.execute_authorized(action)

    assert not again.ok and again.message == "not authorized: the capability was already used"
    assert len(real_writes(env)) == 1


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda env: ROLLBACK, "carries no capability"),
        (lambda env: sign(CapabilityIssuer(RUN, "inc"), ROLLBACK), "signature is not valid"),
        (lambda env: ROLLBACK.model_copy(update={"capability": "x.y"}), "malformed"),
        (lambda env: _expired(env), "expired"),
        (lambda env: _other_run(env), "different run"),
        (lambda env: sign(issuer_for(env), ROLLBACK, safety_enabled=False), "only by a simulation"),
        (
            lambda env: sign(issuer_for(env), ROLLBACK).model_copy(
                update={"params": {"to_version": "v0.9.0"}}
            ),
            "different params",
        ),
    ],
    ids=["none", "forged", "malformed", "expired", "another-run", "safety-off", "altered"],
)
async def test_every_invalid_capability_is_refused_and_nothing_changes(
    tmp_path: Path, make: Any, reason: str
) -> None:
    env = await ready(tmp_path)
    issuer_for(env)  # a key is bound, as after every reset

    outcome = await env.execute_authorized(make(env))

    assert not outcome.ok and reason in outcome.message
    assert sent(env) == []
    assert env.state.writes()[-1]["outcome"] == "refused"
    assert (await env.observe_services()).services[1].version == "v1.1.0"


def _expired(env: KubernetesEnvironment) -> AuthorizedAction:
    issuer = CapabilityIssuer(RUN, "inc", ttl_seconds=-1)
    env.bind_executor(issuer.public_key)
    return sign(issuer, ROLLBACK)


def _other_run(env: KubernetesEnvironment) -> AuthorizedAction:
    issuer = CapabilityIssuer("run-somebody-else", "inc")
    env.bind_executor(issuer.public_key)
    return sign(issuer, ROLLBACK)


async def test_delete_namespace_is_refused_even_with_a_valid_capability(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    issuer = issuer_for(env)
    delete = sign(issuer, AuthorizedAction(action="delete_namespace", target="recap-demo"))

    outcome = await env.execute_authorized(delete)

    assert not outcome.ok and outcome.message.startswith("not supported")
    assert sent(env) == [] and env.state.used_nonces() == set()  # refused before verifying


async def test_an_unknown_target_is_refused_after_the_capability_check(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    payments = AuthorizedAction(
        action="restart", target="payments", approval_id="a", proposal_digest="b"
    )

    outcome = await env.execute_authorized(sign(issuer_for(env), payments))

    assert not outcome.ok and "unknown service 'payments'" in outcome.message
    assert sent(env) == [] and len(env.state.used_nonces()) == 1


async def test_the_write_switch_refuses_writes_and_keeps_the_capability(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    action = sign(issuer_for(env), ROLLBACK)
    disable_writes(env.options.write_switch_path(), "test")

    refused = await env.execute_authorized(action)
    services = await env.observe_services()  # reads keep working
    enable_writes(env.options.write_switch_path())
    accepted = await env.execute_authorized(action)

    assert not refused.ok and "switched off" in refused.message and services.services
    assert accepted.ok and len(real_writes(env)) == 1


async def test_the_nonce_is_burnt_even_when_the_write_then_fails(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    action = sign(issuer_for(env), ROLLBACK)
    forbidden = 'Error from server (Forbidden): deployments.apps "backend" is forbidden'
    env.fake_cluster().failures["rollout_undo:write"] = (1, forbidden)

    # Sent but failed: the outcome is unknown, so it is an error (the controller then lets
    # the oracle judge after the settle wait), never an explicit "refused".
    with pytest.raises(KubeError, match="the write may have reached the cluster") as failed:
        await env.execute_authorized(action)
    retried = await env.execute_authorized(action)

    assert "Forbidden" in str(failed.value)
    assert retried.message == "not authorized: the capability was already used"
    assert [w["outcome"] for w in env.state.writes()] == ["failed", "refused"]
    assert real_writes(env) == []


async def test_a_write_the_read_back_cannot_confirm_is_an_error_for_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await ready(tmp_path)
    action = sign(issuer_for(env), ROLLBACK)
    cluster = env.fake_cluster()
    answer = cluster.handle

    def unavailable_after_the_write(command: Any, identity: str) -> tuple[int, str, str]:
        if command.op == KubeOp.GET_DEPLOYMENT and real_writes(env):
            return 1, "", "Error from server (ServiceUnavailable): try again later"
        return answer(command, identity)

    monkeypatch.setattr(cluster, "handle", unavailable_after_the_write)

    with pytest.raises(KubeError, match="read-back did not confirm it"):
        await env.execute_authorized(action)

    assert [w["outcome"] for w in env.state.writes()] == ["unconfirmed"]
    assert len(real_writes(env)) == 1  # it was sent: whether it worked is the oracle's call


@pytest.mark.parametrize(
    ("version", "reason"),
    [("v9.9.9", "no revision runs v9.9.9"), ("v1.1.0", "already the running revision")],
)
async def test_a_rollback_needs_an_earlier_revision_of_exactly_that_version(
    tmp_path: Path, version: str, reason: str
) -> None:
    env = await ready(tmp_path)
    action = ROLLBACK.model_copy(update={"params": {"to_version": version}})

    outcome = await env.execute_authorized(sign(issuer_for(env), action))

    assert not outcome.ok and reason in outcome.message and sent(env) == []


async def test_a_restart_is_written_and_never_listed_as_a_release(tmp_path: Path) -> None:
    env = await ready(tmp_path)

    outcome = await env.execute_authorized(sign(issuer_for(env), RESTART))

    assert outcome.ok and outcome.message == "backend: rolling restart started"
    assert [c["op"] for c in real_writes(env)] == ["rollout_restart"]
    assert [d.version for d in (await env.observe_deployments("backend", 10)).deployments] == [
        "v1.1.0"
    ]
    assert env.state.release_log()[-1].kind == "restart"


async def test_a_run_that_lost_its_namespace_may_not_write(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    action = sign(issuer_for(env), ROLLBACK)
    later = utcnow() + timedelta(hours=2)  # the first run's lease ran out; another took it
    env.lease.take("recap-demo", "run-kind00002", 600, now=later)

    outcome = await env.execute_authorized(action)

    assert not outcome.ok and "belongs to another run" in outcome.message and sent(env) == []


async def test_no_write_starts_without_time_to_finish_it(tmp_path: Path) -> None:
    env = await ready(tmp_path, write_deadline_seconds=0.01)

    outcome = await env.execute_authorized(sign(issuer_for(env), ROLLBACK))

    assert not outcome.ok and "not enough time left" in outcome.message and sent(env) == []


async def test_the_capability_is_never_stored_anywhere(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    action = sign(issuer_for(env), ROLLBACK)
    await env.execute_authorized(action)
    await env.execute_authorized(action)  # refused: audited too

    for path in (tmp_path / "sandbox").glob("*.db*"):
        assert action.capability.encode() not in path.read_bytes()
        assert action.capability.split(".")[1].encode() not in path.read_bytes()


# --- the oracle ---------------------------------------------------------------------------------


async def test_the_oracle_sees_the_fault_and_then_the_recovery(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    oracle = environment(tmp_path)  # the controller's own instance

    broken = await oracle.health_check()
    await env.execute_authorized(sign(issuer_for(env), ROLLBACK))
    healthy = await oracle.health_check()

    assert set(broken.services) == {"frontend", "backend"}  # exactly the allowlist
    assert broken.services["backend"].error_rate == pytest.approx(0.4)
    assert broken.services["frontend"].error_rate == pytest.approx(0.4)
    assert all(h.status == "Running" and h.error_rate == 0.0 for h in healthy.services.values())
    assert {
        c["identity"]
        for c in oracle.state.commands()
        if c["op"] in ("list_deployments", "list_pods")
    } >= {"oracle"}


async def test_a_rollout_in_progress_is_never_healthy(tmp_path: Path) -> None:
    env = await ready(tmp_path)
    env.fake_cluster().spec()["rollout_seconds"] = 30  # from now on, new pods take 30 s

    outcome = await env.execute_authorized(sign(issuer_for(env), ROLLBACK))
    report = await env.health_check()
    health = await env.observe_health("backend")

    # The rollback was accepted, but the old (bad) pods still serve and a surge pod waits:
    # the oracle never calls that healthy, whatever the replica counts say.
    assert outcome.ok
    assert report.services["backend"].status == "Unknown"
    assert report.services["backend"].ready_replicas == report.services["backend"].replicas == 2
    assert report.services["backend"].error_rate == pytest.approx(0.4)
    assert health.status == "Running"  # an operator's dashboard shows the pods running


async def test_a_crash_looping_release_reads_as_a_crash_and_a_stuck_rollout(tmp_path: Path) -> None:
    env = await ready(tmp_path, "kind-dependency-crash")

    report = await env.health_check()
    health = await env.observe_health("backend")
    events = (await env.observe_events("backend", 30)).events
    logs = await env.observe_logs("backend", 50, 60)

    assert report.services["backend"].status == "CrashLoopBackOff"
    assert health.status == "CrashLoopBackOff" and health.error_rate == 0.0  # old pods serve
    # A start-up error, not an out-of-memory kill: not reported as the memory reason "BackOff".
    assert "CrashLoopBackOff" in {e.reason for e in events} and "BackOff" not in {
        e.reason for e in events
    }
    assert any("[build v1.2.0]" in line.message for line in logs.lines if line.level == "ERROR")


async def test_a_full_memory_shows_in_memory_and_readiness_and_a_restart_clears_it(
    tmp_path: Path,
) -> None:
    env = await ready(tmp_path, "kind-report-leak")

    memory = await env.observe_metrics("backend", "memory_mb", 60)
    with pytest.raises(MetricsUnavailable, match="0 of 2 replicas ready") as unmeasurable:
        await env.observe_health("backend")
    before = await env.health_check()
    outcome = await env.execute_authorized(sign(issuer_for(env), RESTART))
    after = await env.health_check()

    assert memory.points[-1].value / memory.limit >= 0.9  # type: ignore[operator]
    assert "no number is reported" in str(unmeasurable.value)
    assert before.services["backend"].ready_replicas == 0
    assert outcome.ok and all(h.status == "Running" for h in after.services.values())
    assert after.services["backend"].ready_replicas == 2


def test_the_fake_seed_folder_is_labelled_synthetic() -> None:
    for name in ("cluster.json", "backend-snapshot.json"):
        assert "SYNTHETIC" in (FAKE_DATA / name).read_text()
    assert shutil.which  # the fake needs no tool at all: no kubectl, no docker


def test_the_end_of_a_run_gives_the_namespace_lease_up(tmp_path: Path) -> None:
    """An interrupted or finished run must not hold the namespace until its lease expires."""
    engine = environment(tmp_path, run_id="run-one")
    engine.lease.take(engine.namespace, "run-one", 3600)
    other = environment(tmp_path, run_id="run-two")
    with pytest.raises(LeaseHeld):
        other.lease.take(other.namespace, "run-two", 3600)
    engine.end_run()
    other.lease.take(other.namespace, "run-two", 3600)  # free again
