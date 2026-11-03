"""Kubernetes JSON -> RECAP's observation models (recap/environments/kubernetes/translate.py).

The input is hand-written, synthetic kubectl output (tests/fixtures/kind/backend-snapshot.json
and backend-logs.txt, labelled as such): the Kubernetes API reference's shapes, not a
recording. These tests pin the rules a real cluster needs (the M5 audit's gotchas): versions
from image tags, image-pull back-off never reported as "BackOff", OOMKilled read from pod
status and labelled so, log levels from the app's line format, every time in UTC.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from recap.core.signals import OOM_EVENT_REASONS, upstream_dependency, version_tag
from recap.environments.kubernetes import translate
from recap.environments.kubernetes.kubectl import KubeOp, KubeOutputError, parse_list

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kind"
REPOSITORY = "recap-demo"


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    return json.loads((FIXTURES / "backend-snapshot.json").read_text(encoding="utf-8"))


def items(snapshot: dict[str, Any], key: str, kind: str) -> list[dict[str, Any]]:
    """The fixture's list, parsed the way the adapter parses kubectl's output."""
    return parse_list(json.dumps(snapshot[key]), KubeOp.LIST_PODS, kind)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --- Deployments ------------------------------------------------------------------------------


def test_a_deployment_becomes_a_service_with_its_image_tag_as_version(snapshot: dict) -> None:
    deployment = snapshot["deployment"]
    info = translate.service_info(deployment, "recap-demo", ["frontend", "backend"], REPOSITORY)

    assert (info.name, info.version, info.replicas, info.dependencies) == (
        "backend",
        "v1.1.0",
        2,
        [],
    )
    assert translate.selector_of(deployment) == {"app": "backend"}
    assert translate.ready_replicas(deployment) == 2
    assert translate.limit_of(deployment, "backend", REPOSITORY, "memory") == 128 * 2**20
    assert translate.limit_of(deployment, "backend", REPOSITORY, "cpu") == pytest.approx(0.25)


def test_dependencies_come_from_the_annotation_and_stay_inside_the_allowlist() -> None:
    deployment = {
        "metadata": {
            "name": "frontend",
            "annotations": {"recap.dev/depends-on": "backend, payments,Bad Name,backend"},
        }
    }
    assert translate.dependencies(deployment, ["frontend", "backend"]) == ["backend"]


@pytest.mark.parametrize(
    ("image", "version"),
    [
        ("recap-demo/backend:v1.1.0", "v1.1.0"),
        ("registry.local:5000/recap-demo/backend:v2.0.10", "v2.0.10"),
        ("recap-demo/backend:latest", None),
        ("recap-demo/backend:1.1.0", None),
        ("recap-demo/backend@sha256:" + "a" * 64, None),
        ("recap-demo/backend", None),
        (None, None),
    ],
)
def test_only_a_vxyz_tag_is_a_version(image: str | None, version: str | None) -> None:
    assert translate.version_of(image) == version


@pytest.mark.parametrize(
    ("image", "version"),
    [
        ("ghcr.io/sregym/hotel-reservation:20260912.1@sha256:" + "8" * 64, "20260912.1"),
        ("ghcr.io/sregym/hotel-reservation:sha-d2c036bc3d11", "sha-d2c036bc3d11"),
        ("recap-demo/backend:v1.1.0", "v1.1.0"),
        ("registry.local:5000/app:latest", "latest"),
        ("registry.local:5000/app", None),  # a port, no tag
        ("ghcr.io/sregym/hotel-reservation@sha256:" + "8" * 64, None),  # a digest alone
        ("app:-bad", None),
        (None, None),
    ],
)
def test_in_the_tag_format_any_tag_is_the_release(image: str | None, version: str | None) -> None:
    """M5 (SREGym): date and commit tags, also when a digest pins the image."""
    assert translate.version_of(image, "tag") == version
    assert translate.valid_release(version, "tag") is (version is not None)


def test_a_release_shape_follows_the_targets_format() -> None:
    assert translate.valid_release("v1.0.0") and not translate.valid_release("20260912.1")
    assert translate.valid_release("20260912.1", "tag")
    assert not translate.valid_release("1.0 && rm", "tag") and not translate.valid_release(None)


def test_documented_dependencies_fill_in_for_a_missing_annotation(snapshot: dict) -> None:
    """A target whose manifests carry no depends-on annotation (SREGym's hotel reservation)
    takes its call graph from the profile: the app's architecture, nothing about a fault."""
    plain = json.loads(json.dumps(snapshot["deployment"]))
    plain["metadata"].get("annotations", {}).pop(translate.DEPENDS_ON, None)
    documented = {"backend": ["frontend", "payments", "backend"]}
    allowed = ["frontend", "backend"]
    info = translate.service_info(plain, "demo", allowed, REPOSITORY, "semver", documented)
    assert info.dependencies == ["frontend"]  # allowlisted services only, never itself


def test_a_selector_the_adapter_did_not_build_from_plain_labels_is_refused() -> None:
    expressions = {
        "metadata": {"name": "backend"},
        "spec": {
            "selector": {
                "matchLabels": {"app": "backend"},
                "matchExpressions": [{"key": "tier", "operator": "In", "values": ["a"]}],
            }
        },
    }
    with pytest.raises(KubeOutputError, match="no plain label selector"):
        translate.selector_of(expressions)
    odd = {"metadata": {"name": "backend"}, "spec": {"selector": {"matchLabels": {"app": "a,b"}}}}
    with pytest.raises(KubeOutputError, match="invalid selector"):
        translate.selector_of(odd)


@pytest.mark.parametrize(
    ("quantity", "value"),
    [
        ("128Mi", 128 * 2**20),
        ("1Gi", 2**30),
        ("500M", 5e8),
        ("250m", 0.25),
        ("2", 2.0),
        ("1.5", 1.5),
    ],
)
def test_kubernetes_quantities(quantity: str, value: float) -> None:
    assert translate.parse_quantity(quantity) == pytest.approx(value)


@pytest.mark.parametrize("quantity", ["", "lots", "12Qi", None, "-1"])
def test_an_unreadable_quantity_is_none_never_a_guess(quantity: str | None) -> None:
    assert translate.parse_quantity(quantity) is None


# --- status -----------------------------------------------------------------------------------


def pod(*, waiting: str | None = None, ready: bool = True, phase: str = "Running") -> dict:
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {
        "metadata": {"name": "backend-1-abcde"},
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [{"name": "backend", "state": state, "restartCount": 2}],
        },
    }


def test_the_agents_status_is_what_a_dashboard_shows() -> None:
    deployment = {"metadata": {"name": "backend"}}
    assert translate.agent_status(None, [], stuck=False) == "Deleted"
    assert translate.agent_status(deployment, [pod(waiting="CrashLoopBackOff")], False) == (
        "CrashLoopBackOff"
    )
    assert translate.agent_status(deployment, [pod(), pod(waiting="ImagePullBackOff")], False) == (
        "Unknown"
    )
    assert translate.agent_status(deployment, [pod()], stuck=True) == "Unknown"
    assert translate.agent_status(deployment, [pod(phase="Pending")], False) == "Unknown"
    assert translate.agent_status(deployment, [pod(ready=False)], False) == "Running"


def test_the_oracles_status_is_running_only_when_the_rollout_is_complete() -> None:
    deployment = {"metadata": {"name": "backend"}}
    assert translate.ground_status(deployment, [pod()], rollout_complete=True) == "Running"
    assert translate.ground_status(deployment, [pod()], rollout_complete=False) == "Unknown"
    crash = [pod(waiting="CrashLoopBackOff")]
    assert translate.ground_status(deployment, crash, rollout_complete=True) == "CrashLoopBackOff"
    assert translate.ground_status(None, [], rollout_complete=True) == "Deleted"
    assert translate.restart_count([pod(), pod()]) == 4


# --- events -----------------------------------------------------------------------------------


def test_events_keep_the_real_reason_count_object_and_utc_time(snapshot: dict) -> None:
    raw = items(snapshot, "events", "Event")
    scaling = translate.cluster_event(raw[0], "backend")
    unhealthy = translate.cluster_event(raw[2], "backend")

    assert scaling is not None and unhealthy is not None
    assert (scaling.reason, scaling.object, scaling.type) == (
        "ScalingReplicaSet",
        "deployment/backend",
        "Normal",
    )
    assert (unhealthy.reason, unhealthy.count, unhealthy.type) == ("Unhealthy", 4, "Warning")
    assert unhealthy.timestamp == at("2026-11-02T09:55:00")
    # events.k8s.io style: the series' last time and count win over eventTime.
    series = translate.cluster_event(raw[4], "backend")
    assert series is not None and series.count == 3
    assert series.timestamp == at("2026-11-02T09:58:00")


def test_an_aggregated_event_keeps_when_it_first_happened(snapshot: dict) -> None:
    """A crash loop's "Back-off ... x20" began long before its latest occurrence (M5: the
    verifier dates a bad release's onset by it on a target without metrics)."""
    raw = items(snapshot, "events", "Event")
    unhealthy, series, started = (translate.cluster_event(raw[i], "backend") for i in (2, 4, 5))
    assert unhealthy is not None and series is not None and started is not None
    assert (unhealthy.first_seen, unhealthy.timestamp) == (
        at("2026-11-02T09:52:00"),
        at("2026-11-02T09:55:00"),
    )
    assert series.first_seen == at("2026-11-02T09:50:01.123456")  # events.k8s.io: eventTime
    assert started.first_seen is None  # only a last time: nothing is guessed


def test_image_pull_back_off_is_never_reported_as_back_off(snapshot: dict) -> None:
    pulling = translate.cluster_event(items(snapshot, "events", "Event")[1], "backend")

    assert pulling is not None and pulling.reason == "ImagePullBackOff"
    # RECAP's rules count "BackOff" as memory evidence: a pull problem must not look like one.
    assert pulling.reason not in OOM_EVENT_REASONS


def test_a_crash_loop_after_an_error_is_not_memory_evidence_but_one_after_oom_is() -> None:
    event = {
        "reason": "BackOff",
        "message": "Back-off restarting failed container backend in pod backend-1-abcde",
        "type": "Warning",
        "involvedObject": {"kind": "Pod", "name": "backend-1-abcde"},
        "lastTimestamp": "2026-11-02T10:00:00Z",
    }
    after_error = translate.cluster_event(event, "backend", {"Error"})
    after_oom = translate.cluster_event(event, "backend", {"OOMKilled"})
    unknown = translate.cluster_event(event, "backend")

    assert after_error is not None and after_error.reason == "CrashLoopBackOff"
    assert after_oom is not None and after_oom.reason == "BackOff"
    assert unknown is not None and unknown.reason == "BackOff"  # the pod is gone: as reported


def test_oom_kills_are_read_from_pod_status_and_say_so(snapshot: dict) -> None:
    pods = items(snapshot, "pods", "Pod")
    [kill] = translate.oom_kills(pods, "backend")

    assert (kill.reason, kill.type, kill.object) == (
        "OOMKilled",
        "Warning",
        "pod/backend-6a1b2c3d4e-x7k2p",
    )
    assert kill.timestamp == at("2026-11-02T09:40:00")
    assert "pod's container status, not from a Kubernetes event" in kill.message
    assert translate.last_terminations(pods)["backend-6a1b2c3d4e-x7k2p"] == {"OOMKilled"}


def test_an_event_without_a_time_or_an_object_is_dropped() -> None:
    assert translate.cluster_event({"reason": "X", "involvedObject": {"kind": "Pod"}}, "b") is None
    no_time = {"reason": "X", "involvedObject": {"kind": "Pod", "name": "p"}}
    assert translate.cluster_event(no_time, "backend") is None


# --- logs -------------------------------------------------------------------------------------


def test_log_lines_take_level_and_time_from_the_apps_own_format() -> None:
    text = (FIXTURES / "backend-logs.txt").read_text(encoding="utf-8")
    lines = translate.log_lines("backend", text)

    assert [line.level for line in lines] == [
        "INFO",
        "ERROR",
        "WARN",  # WARNING
        "INFO",  # a traceback line: no level of its own, the kubelet's time
        "ERROR",  # FATAL
        "ERROR",  # CRITICAL
        "ERROR",  # PANIC
        "DEBUG",
        "INFO",  # a word that is not a level: kept as text
    ]
    assert lines[0].timestamp == at("2026-11-02T09:50:05.120000")  # the app's time, in UTC
    assert lines[0].message == "serving on :8080 [build v1.1.0]"
    assert lines[3].timestamp == at("2026-11-02T09:51:20")
    assert lines[3].message == "Traceback (most recent call last):"
    assert all(line.service == "backend" for line in lines)
    # The phrasing RECAP's parsers read (recap/core/signals.py) survives the translation.
    assert version_tag(lines[1].message) == "v1.1.0"


def test_zerolog_console_lines_keep_their_level_without_their_colours() -> None:
    """DeathStarBench's Go services log through zerolog's console writer: RFC 3339 time, a
    three-letter level, colours on by default (SREGym's hotel reservation)."""
    grey, red, reset = "\x1b[90m", "\x1b[31m", "\x1b[0m"
    text = (
        "[pod/geo-6b9d-xk2lp/hotel-reserv-geo] 2026-11-03T15:58:40.120000000Z "
        f"{grey}2026-11-03T15:58:40Z{reset} {red}PNC{reset} "
        "cmd/geo/db.go:30 > no reachable servers\n"
        "[pod/geo-6b9d-xk2lp/hotel-reserv-geo] 2026-11-03T15:58:39.000000000Z "
        f"{grey}2026-11-03T15:58:39Z{reset} INF cmd/geo/main.go:40 > Reading config...\n"
    )
    panic, info = translate.log_lines("geo", text)
    assert (panic.level, info.level) == ("ERROR", "INFO")
    assert panic.message == "cmd/geo/db.go:30 > no reachable servers"
    assert "\x1b" not in panic.message + info.message
    assert panic.timestamp == at("2026-11-03T15:58:40")


def test_the_demo_apps_rpc_phrasing_is_what_the_parsers_read() -> None:
    text = (
        "[pod/frontend-1-abcde/frontend] 2026-11-02T10:00:00.456000000Z "
        "2026-11-02T10:00:00.456Z ERROR rpc to backend failed: DEADLINE_EXCEEDED "
        "(backend.recap-demo:80/items, 2000 ms)\n"
        "[pod/frontend-1-abcde/frontend] 2026-11-02T10:00:01Z 2026-11-02T10:00:01.000Z "
        "ERROR rpc to backend failed: UNAVAILABLE (connection refused)\n"
    )
    first, second = translate.log_lines("frontend", text)

    assert upstream_dependency(first.message) == "backend"
    assert upstream_dependency(second.message) == "backend"
    assert first.timestamp == at("2026-11-02T10:00:00.456000")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-02T10:00:00.123456789Z", at("2026-11-02T10:00:00.123456")),
        ("2026-11-02T12:00:00+02:00", at("2026-11-02T10:00:00")),
        ("2026-11-02T10:00:00Z", at("2026-11-02T10:00:00")),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_times_are_parsed_to_utc_with_nanoseconds_cut(text: str | None, expected: Any) -> None:
    assert translate.parse_time(text) == expected
