"""The Kubernetes adapter in external-harness mode (M5 W5: SREGym's external-harness mode),
over the offline fake cluster.

A benchmark harness deployed the app and injected the fault before the run: reset changes
nothing, takes each Deployment's first revision as the run's baseline and reads only as
recap-oracle. The images carry date and commit tags pinned by digests (release format "tag"),
and there is no Prometheus and no probe URL: health is status and readiness, and an approved
rollback restores exactly the image of the revision its tag names. The seed
(tests/fixtures/sregym/cluster.json) is synthetic.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from recap.core.config import REPO_ROOT
from recap.core.errors import ConfigError
from recap.core.ids import utcnow
from recap.core.observations import AlertSignal, AuthorizedAction, LogLine
from recap.environments.kubernetes import KubernetesEnvironment, engine
from recap.environments.kubernetes.metrics import MetricsUnavailable
from tests.helpers import issuer_for, sign
from tests.unit.test_kind_engine import kind_options

DATA = REPO_ROOT / "tests" / "fixtures" / "sregym"
SERVICES = ["frontend", "geo", "mongodb-geo"]
GOOD, BAD = "sha-1f2e3d4c5b6a", "20260101.1"
GOOD_IMAGE = f"ghcr.io/example/hotel-reservation:{GOOD}@sha256:{'1' * 64}"
RUN = "run-ext00001"
WRITES = ("rollout_undo", "rollout_restart", "set_image")


def options(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    return {
        "scenarios_dir": str(DATA / "scenarios"),
        "state_dir": str(tmp_path / "sandbox"),
        "client": "fake",
        "fake_data_dir": str(DATA),
        "harness": "external",
        "release_format": "tag",
        "namespace": "hotel-reservation",
        "services": SERVICES,
        "image_repository": "ghcr.io/example/hotel-reservation",
        "service_urls": {},
        "probe_paths": {},
        "prometheus_url": None,
        "dependencies": {"frontend": ["geo"], "geo": ["mongodb-geo"]},
        "probe_round_gap_seconds": 0.0,
        "alert_poll_seconds": 0.01,
        "stabilization_poll_seconds": 0.1,
        **changes,
    }


def environment(tmp_path: Path, **changes: Any) -> KubernetesEnvironment:
    return KubernetesEnvironment(RUN, **options(tmp_path, **changes))


async def ready(tmp_path: Path) -> tuple[KubernetesEnvironment, AlertSignal]:
    env = environment(tmp_path)
    return env, await env.reset("test-external-geo", 11)


def rollback(version: object) -> AuthorizedAction:
    return AuthorizedAction(
        action="rollback",
        target="geo",
        params={"to_version": version},
        approval_id="apr-ext-1",
        proposal_digest="d" * 64,
    )


# --- reset: nothing is changed, the alert is measured ----------------------------------------


async def test_reset_writes_nothing_and_alerts_on_the_crash_loop(tmp_path: Path) -> None:
    env, alert = await ready(tmp_path)

    assert not [c for c in env.state.commands() if c["op"] in WRITES]
    assert {c["identity"] for c in env.state.commands()} == {"reader", "oracle"}
    # The alert names what crashed, from the cluster's own state: no probe, no label.
    assert alert.service == "geo" and alert.affected_services == ["geo"]
    assert any("CrashLoopBackOff" in symptom for symptom in alert.symptoms)
    info = env.state.info()
    assert {s: b.revision for s, b in info.baselines.items()} == {
        "frontend": 1,
        "geo": 1,
        "mongodb-geo": 1,
    }
    assert info.baselines["geo"].version == GOOD


async def test_the_harness_fault_is_a_release_with_its_restore_point(tmp_path: Path) -> None:
    env, _ = await ready(tmp_path)

    [release] = (await env.observe_deployments("geo", 5)).deployments
    assert (release.version, release.previous_version) == (BAD, GOOD)
    assert release.replicaset is not None and release.replicaset.startswith("geo-")
    assert release.deployed_at > env.state.info().baseline_at  # minutes after the app's deploy
    plan = await env.plan_rollback("geo")
    assert (plan.possible, plan.current_version, plan.target_version) == (True, BAD, GOOD)
    services = {s.name: s for s in (await env.observe_services()).services}
    assert services["geo"].version == BAD
    assert services["geo"].dependencies == ["mongodb-geo"]  # the documented call graph


async def test_a_scenario_for_the_other_harness_is_refused(tmp_path: Path) -> None:
    """A RECAP-injected scenario never runs on an external target, nor the other way round."""
    external = environment(tmp_path, scenarios_dir=str(REPO_ROOT / "fixtures" / "kind-scenarios"))
    with pytest.raises(ValueError, match="is for RECAP-injected faults"):
        await external.reset("kind-bad-release", 7)
    demo = KubernetesEnvironment(
        RUN, **kind_options(tmp_path / "demo", scenarios_dir=str(DATA / "scenarios"))
    )
    with pytest.raises(ValueError, match="is for an external harness"):
        await demo.reset("test-external-geo", 11)
    assert not external.state.commands() and not demo.state.commands()  # refused before reads


# --- reads without a monitoring system ------------------------------------------------------


async def test_health_says_what_is_not_measured(tmp_path: Path) -> None:
    env, _ = await ready(tmp_path)

    geo = await env.observe_health("geo")
    assert (geo.error_rate, geo.latency_p95_ms) == (None, None)
    assert geo.status != "Running" or geo.ready_replicas < geo.replicas
    with pytest.raises(MetricsUnavailable, match="exports no error_rate metric for geo"):
        await env.observe_metrics("geo", "error_rate", 30)
    restarts = await env.observe_metrics("geo", "restarts", 30)  # read from pod status
    assert restarts.points and restarts.points[-1].value >= 0
    report = await env.health_check()
    assert {h.error_rate for h in report.services.values()} == {None}  # no probe: no number


async def test_a_crash_looping_pods_lines_are_read_once(tmp_path: Path) -> None:
    """A container waiting to restart answers the plain read with its last run's lines, which
    the --previous read returns too (seen on SREGym's geo): each of the pod's lines once."""
    env, _ = await ready(tmp_path)
    logs = await env.observe_logs("geo", 100, 60)

    keys = [(line.timestamp, line.level, line.message) for line in logs.lines]
    assert logs.lines and len(keys) == len(set(keys)) and logs.total_matched == len(keys)
    reads = [c["argv"] for c in env.state.commands() if c["op"] == "pod_logs"]
    assert any("--previous" in argv for argv in reads)  # both reads were made


async def test_lines_repeated_within_one_read_are_all_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only what the --previous read repeats from the plain read goes: a threaded app can log
    the same line twice in one millisecond, and both are lines."""
    env, _ = await ready(tmp_path)
    line = LogLine(timestamp=utcnow(), level="ERROR", service="geo", message="dial failed")

    async def reads(*args: Any) -> tuple[list[LogLine], bool]:
        previous = args[-1]
        return ([line] * 3 if previous else [line] * 2), False

    monkeypatch.setattr(env, "_pod_lines", reads)
    logs = await env.observe_logs("geo", 100, 60)
    assert len(logs.lines) == logs.total_matched == 3  # the plain read's two, one more before


# --- the write: a release tag selects a revision of the Deployment's own history --------------


async def test_an_approved_rollback_restores_the_tagged_revision_exactly(tmp_path: Path) -> None:
    env, _ = await ready(tmp_path)
    issuer = issuer_for(env)

    outcome = await env.execute_authorized(sign(issuer, rollback(GOOD)))

    assert outcome.ok, outcome.message
    assert f"geo pod template image: {GOOD_IMAGE}" in outcome.side_effects  # digest included
    [write] = [w for w in env.state.writes() if w["outcome"] == "executed"]
    assert write["detail"]["image_after"] == GOOD_IMAGE and write["detail"]["to_revision"] == 1


@pytest.mark.parametrize(
    ("version", "why"),
    [
        ("v1.0.0 latest", "needs a target version image tag"),  # not an image tag
        ("", "needs a target version image tag"),
        ("sha-0000000", "no revision runs sha-0000000"),  # a tag the history does not hold
        (BAD, "already the running revision"),
    ],
)
async def test_a_rollback_to_anything_else_is_refused(
    tmp_path: Path, version: str, why: str
) -> None:
    env, _ = await ready(tmp_path)
    outcome = await env.execute_authorized(sign(issuer_for(env), rollback(version)))
    assert not outcome.ok and why in outcome.message
    assert not [c for c in env.state.commands() if c["op"] in WRITES]


# --- the oracle without a probe --------------------------------------------------------------


def crash_lasting(tmp_path: Path, seconds: int) -> Path:
    """The seed with each start of geo's bad image running `seconds` before it crashes."""
    folder = tmp_path / "fake-data"
    folder.mkdir()
    spec = json.loads((DATA / "cluster.json").read_text())
    [bad] = [image for image, behaviour in spec["images"].items() if behaviour.get("crash")]
    spec["images"][bad]["crash_after_seconds"] = seconds
    (folder / "cluster.json").write_text(json.dumps(spec))
    return folder


async def test_a_crash_loop_never_reads_as_recovered_while_a_start_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a probe the oracle has status and readiness, and a crash-looping container with
    no readiness probe is Running and Ready while each start lasts (SREGym's geo: until its
    database dial gives up). Healthy needs containers that ran a minute, none exiting in five."""
    clock = [utcnow()]
    monkeypatch.setattr(engine, "utcnow", lambda: clock[0])
    env = environment(tmp_path, fake_data_dir=str(crash_lasting(tmp_path, 10)))
    env.fake_cluster().clock = lambda: clock[0]
    env.fake_cluster().state()  # seeded: geo's release started 6 crash cycles (50 s) ago
    seeded = clock[0]

    def at(seconds: int) -> None:
        clock[0] = seeded + timedelta(seconds=seconds)

    at(20)  # waiting to restart
    await env.reset("test-external-geo", 11)
    at(55)  # 5 s into a start: Running and Ready, the rollout complete
    assert (await env.observe_health("geo")).status == "CrashLoopBackOff"  # up 5 s since a crash
    assert (await env.health_check()).services["geo"].status == "Unknown"

    at(60)
    assert (await env.execute_authorized(sign(issuer_for(env), rollback(GOOD)))).ok
    assert (await env.health_check()).services["geo"].status == "Unknown"  # its pod just started
    at(60 + 61)
    report = await env.health_check()
    assert {h.status for h in report.services.values()} == {"Running"}


async def test_the_alert_names_a_crash_loop_even_while_a_start_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seen in M5 (run-69e580846e): the alert was measured while geo was briefly up between
    two crashes, so it said only "readiness failing"; the runbook search then found readiness
    and memory guides, and the model blamed memory. A container that has not stayed up a
    minute since it restarted after an error exit is in a crash loop, whatever the instant
    shows."""
    clock = [utcnow()]
    monkeypatch.setattr(engine, "utcnow", lambda: clock[0])
    env = environment(tmp_path, fake_data_dir=str(crash_lasting(tmp_path, 10)))
    env.fake_cluster().clock = lambda: clock[0]
    env.fake_cluster().state()  # seeded: geo's release started 6 crash cycles (50 s) ago
    clock[0] += timedelta(seconds=55)  # 5 s into a start: Running and Ready

    alert = await env.reset("test-external-geo", 11)

    assert alert.service == "geo"
    assert any("CrashLoopBackOff (crash loop)" in symptom for symptom in alert.symptoms)


# --- options ---------------------------------------------------------------------------------


def test_only_the_external_harness_may_go_without_probes_or_metrics(tmp_path: Path) -> None:
    assert environment(tmp_path).options.prometheus_url is None
    with pytest.raises(ConfigError, match="service_urls must name exactly"):
        environment(tmp_path, harness="recap", prometheus_url="http://127.0.0.1:9090")
    with pytest.raises(ConfigError, match="harness 'recap' needs prometheus_url"):
        environment(
            tmp_path,
            harness="recap",
            service_urls={s: f"http://127.0.0.1:{30080 + i}" for i, s in enumerate(SERVICES)},
        )
    with pytest.raises(ConfigError, match="dependencies may name only allowed services"):
        environment(tmp_path, dependencies={"geo": ["payments"]})
    with pytest.raises(ConfigError, match="service_urls may name only allowed services"):
        environment(tmp_path, service_urls={"payments": "http://127.0.0.1:30080"})
