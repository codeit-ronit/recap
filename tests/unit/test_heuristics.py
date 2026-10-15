"""Tests for the scripted policy's hypothesis heuristics (recap/providers/heuristics.py).

Evidence is built the way the tool gateway stores it: an observation model from
recap/core/observations.py, dumped with model_dump(mode="json") into Evidence.data and
recorded in an EvidenceRegistry under its canonical source_ref.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from recap.context.evidence import EvidenceRegistry, tool_source_ref
from recap.core.models import (
    ActionRecord,
    EpisodeOutcome,
    Evidence,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    Incident,
    MemoryHit,
    MemoryPartition,
    RunBudget,
    SourceType,
)
from recap.core.observations import (
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from recap.core.signals import OOM_ERROR_MESSAGE, RELEASE_ERROR_TEMPLATE, UPSTREAM_ERROR_TEMPLATE
from recap.providers.heuristics import generate_hypotheses, parse_evidence

M = FailureMechanism
T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)  # alert time = simulated minute 0
NPE = RELEASE_ERROR_TEMPLATE.format(exception="NullPointerException", version="v2.4.1")
# A UTC timestamp as pydantic writes it in JSON, e.g. "2026-03-14T10:00:00Z".
UTC_TIMESTAMP = re.compile(r'(T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z"')


def at(minute: float) -> datetime:
    """Simulated time, in minutes relative to the alert."""
    return T0 + timedelta(minutes=minute)


def upstream(dependency: str) -> str:
    return UPSTREAM_ERROR_TEMPLATE.format(dependency=dependency, reason="timeout after 3000ms")


def make_incident(service: str = "checkout", affected: list[str] | None = None) -> Incident:
    return Incident(
        incident_id="inc-1",
        scenario_id="unit",
        service=service,
        affected_services=affected or [service],
        symptoms=[f"{service}: HTTP 5xx ratio above 30% (SLO 1%)"],
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )


def new_registry() -> EvidenceRegistry:
    return EvidenceRegistry("run-unit01", "inc-1")


def record(
    registry: EvidenceRegistry,
    tool: str,
    observation: BaseModel,
    *,
    utc_suffix: str = "Z",
    **args: Any,
) -> Evidence:
    """Store an observation model as tool evidence, exactly like the gateway does.

    `utc_suffix` is how the stored timestamps end: "Z", "+00:00" or "" (no offset at all).
    """
    content = UTC_TIMESTAMP.sub(rf'\1{utc_suffix}"', observation.model_dump_json())
    return registry.add(
        source_type=SourceType.TOOL,
        source_ref=tool_source_ref(tool, args),
        content=content,
        data=json.loads(content),
        tool_name=tool,
    )


# --- observation builders (real observation models, so shapes match the gateway's) -------


def releases(*items: tuple[str, str, float, str | None]) -> DeploymentList:
    """(service, version, minute, previous_version) records, newest first."""
    return DeploymentList(
        deployments=[
            DeploymentRecord(service=s, version=v, deployed_at=at(m), previous_version=p)
            for s, v, m, p in items
        ]
    )


def logs(service: str, *lines: tuple[float, Any, str]) -> LogQueryResult:
    """(minute, level, message) lines, chronological."""
    return LogQueryResult(
        service=service,
        lines=[
            LogLine(timestamp=at(m), level=level, service=service, message=message)
            for m, level, message in lines
        ],
        total_matched=len(lines),
    )


def series(
    service: str, metric: Any, values: list[float], limit: float | None = None
) -> MetricSeries:
    """One point every 5 minutes; the last point is at minute 0."""
    start = -5 * (len(values) - 1)
    points = [MetricPoint(timestamp=at(start + 5 * i), value=v) for i, v in enumerate(values)]
    return MetricSeries(service=service, metric=metric, unit="", points=points, limit=limit)


def health(service: str, error_rate: float = 0.0, minute: float = 0) -> HealthSnapshot:
    return HealthSnapshot(
        service=service,
        status="Running",
        ready_replicas=2,
        replicas=2,
        error_rate=error_rate,
        latency_p95_ms=90.0,
        observed_at=at(minute),
    )


def events(*items: tuple[str, str, float]) -> EventList:
    """(service, reason, minute) events, newest first."""
    return EventList(
        events=[
            ClusterEvent(
                timestamp=at(m),
                type="Normal" if reason == "ScalingReplicaSet" else "Warning",
                reason=reason,
                object=f"pod/{service}-5d8f7-x2k9q",
                service=service,
                message=f"{reason} for {service}",
            )
            for service, reason, m in items
        ]
    )


def memory_hit(
    mechanism: FailureMechanism | None = M.BAD_DEPLOYMENT,
    target: str | None = "checkout",
    outcome: EpisodeOutcome = EpisodeOutcome.SUCCESS,
    episode_id: str = "ep-1",
) -> MemoryHit:
    success = outcome == EpisodeOutcome.SUCCESS
    episode = ExperienceEpisode(
        episode_id=episode_id,
        origin_run_id="run-old",
        scenario_id="bad-deployment",
        failure_family="bad_deployment",
        partition=MemoryPartition.DEMO,
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        failure_mechanism=mechanism,
        mechanism_target=target,
        successful_action=(
            ActionRecord(action="execute_rollback", target="checkout", result="recovered")
            if success
            else None
        ),
        outcome=outcome,
        verified=success,
        created_at=at(-3000),
    )
    return MemoryHit(episode=episode, similarity=0.9, outcome=outcome)


def by_mechanism(hypotheses: list, mechanism: FailureMechanism) -> list:
    return [h for h in hypotheses if h.mechanism == mechanism]


# --- H-deploy -------------------------------------------------------------------------------


def test_recent_release_with_errors_is_a_bad_deployment() -> None:
    registry = new_registry()
    deploys = record(
        registry,
        "get_recent_deployments",
        releases(("checkout", "v2.4.1", -12, "v2.4.0"), ("checkout", "v2.4.0", -2880, None)),
        service="checkout",
    )
    error_logs = record(
        registry,
        "get_service_logs",
        logs("checkout", *[(m, "ERROR", NPE) for m in range(-10, 1)]),
        service="checkout",
    )
    error_rate = record(
        registry,
        "get_service_metrics",
        series("checkout", "error_rate", [0.002] * 10 + [0.33, 0.32, 0.34]),
        service="checkout",
        metric="error_rate",
    )

    [hypothesis] = by_mechanism(
        generate_hypotheses(registry.all(), make_incident()), M.BAD_DEPLOYMENT
    )

    assert hypothesis.target_service == "checkout"
    assert hypothesis.origin == "investigation"
    assert hypothesis.details == {"version": "v2.4.1", "deployed_at": at(-12).isoformat()}
    assert hypothesis.claim == (
        "checkout v2.4.1, deployed 12 min before the alert, may have introduced the errors"
    )
    assert hypothesis.supporting_evidence_ids == [deploys.id, error_logs.id, error_rate.id]


def test_stale_release_with_errors_still_gives_the_naive_hypothesis() -> None:
    # Released 90 min ago but errors began only 10 min ago: the timing does not fit, yet the
    # recall-oriented heuristic still proposes it. Rejecting it is the verifier's job.
    registry = new_registry()
    record(registry, "get_recent_deployments", releases(("checkout", "v2.4.1", -90, "v2.4.0")))
    record(registry, "get_service_health", health("checkout", error_rate=0.3), service="checkout")

    [hypothesis] = by_mechanism(
        generate_hypotheses(registry.all(), make_incident()), M.BAD_DEPLOYMENT
    )
    assert hypothesis.details["version"] == "v2.4.1"
    assert "90 min before the alert" in hypothesis.claim


def test_old_release_or_no_errors_gives_no_bad_deployment() -> None:
    old = new_registry()
    record(old, "get_recent_deployments", releases(("checkout", "v2.4.0", -2880, None)))
    record(old, "get_service_health", health("checkout", error_rate=0.3), service="checkout")
    assert not by_mechanism(generate_hypotheses(old.all(), make_incident()), M.BAD_DEPLOYMENT)

    quiet = new_registry()
    record(quiet, "get_recent_deployments", releases(("checkout", "v2.4.1", -12, "v2.4.0")))
    record(quiet, "get_service_health", health("checkout", error_rate=0.01), service="checkout")
    assert generate_hypotheses(quiet.all(), make_incident()) == []


def test_now_is_the_newest_timestamp_in_the_evidence() -> None:
    # 100 min before the alert is "recent", but the newest observation is 30 min after the
    # alert (a later epoch), so the release is 130 min old by then.
    registry = new_registry()
    record(registry, "get_recent_deployments", releases(("checkout", "v2.4.1", -100, "v2.4.0")))
    record(registry, "get_service_health", health("checkout", 0.3, minute=30), service="checkout")

    assert parse_evidence(registry.all()).reference_time(T0) == at(30)
    assert not by_mechanism(generate_hypotheses(registry.all(), make_incident()), M.BAD_DEPLOYMENT)

    # The alert time is a floor: an old release list alone does not make its newest entry "now".
    old = new_registry()
    record(old, "get_recent_deployments", releases(("checkout", "v2.4.0", -2880, None)))
    assert parse_evidence(old.all()).reference_time(T0) == T0
    assert parse_evidence([]).reference_time(T0) == T0


def test_timestamps_without_an_offset_count_as_utc() -> None:
    # An adapter may leave out the UTC offset, and the alert time may be naive: both mean UTC.
    def bad_deployment(utc_suffix: str, alert: datetime) -> list[Hypothesis]:
        registry = new_registry()
        release = releases(("checkout", "v2.4.1", -12, "v2.4.0"))
        record(registry, "get_recent_deployments", release, utc_suffix=utc_suffix)
        snapshot = health("checkout", error_rate=0.3, minute=5)
        record(registry, "get_service_health", snapshot, utc_suffix=utc_suffix, service="checkout")
        incident = make_incident().model_copy(update={"created_at": alert})
        return generate_hypotheses(registry.all(), incident)

    explicit = bad_deployment("+00:00", T0)
    [hypothesis] = explicit
    assert hypothesis.details["deployed_at"] == at(-12).isoformat()
    for alert in (T0, T0.replace(tzinfo=None)):  # naive evidence, aware or naive alert
        assert bad_deployment("", alert) == explicit


# --- H-resource and H-dependency ------------------------------------------------------------


def test_oom_signals_suggest_resource_pressure() -> None:
    registry = new_registry()
    kills = record(
        registry,
        "get_k8s_like_events",
        events(
            ("payment", "OOMKilled", -2),
            ("payment", "BackOff", -1),
            ("cart", "ScalingReplicaSet", -50),
        ),
    )
    memory = record(
        registry,
        "get_service_metrics",
        series("payment", "memory_mb", [300, 420, 505], limit=512),
        service="payment",
        metric="memory_mb",
    )
    oom_logs = record(
        registry,
        "get_service_logs",
        logs("payment", (-1, "ERROR", OOM_ERROR_MESSAGE)),
        service="payment",
    )

    hypotheses = generate_hypotheses(registry.all(), make_incident("payment"))

    [hypothesis] = by_mechanism(hypotheses, M.RESOURCE_PRESSURE)
    assert hypothesis.target_service == "payment"
    assert hypothesis.supporting_evidence_ids == [kills.id, memory.id, oom_logs.id]
    assert hypothesis.claim == (
        "payment may be under memory pressure: OOMKilled/BackOff events, memory at or above "
        "90% of its limit, out-of-memory errors in its logs"
    )


def test_relaxed_memory_and_normal_events_give_no_resource_pressure() -> None:
    registry = new_registry()
    record(registry, "get_k8s_like_events", events(("checkout", "ScalingReplicaSet", -12)))
    record(
        registry,
        "get_service_metrics",
        series("checkout", "memory_mb", [200, 205, 210], limit=512),
        service="checkout",
        metric="memory_mb",
    )
    assert generate_hypotheses(registry.all(), make_incident()) == []


def test_upstream_errors_suggest_a_dependency_failure() -> None:
    registry = new_registry()
    caller_logs = record(
        registry,
        "get_service_logs",
        logs(
            "checkout",
            (-3, "ERROR", upstream("payment")),
            (-2, "WARN", upstream("cart")),  # warnings are not failures
            (-1, "ERROR", upstream("payment")),
        ),
        service="checkout",
    )

    [hypothesis] = by_mechanism(
        generate_hypotheses(registry.all(), make_incident()), M.DEPENDENCY_FAILURE
    )

    assert hypothesis.target_service == "checkout"
    assert hypothesis.details == {"dependency": "payment"}
    assert hypothesis.supporting_evidence_ids == [caller_logs.id]
    assert "upstream payment call failed" in hypothesis.claim


def test_one_hypothesis_per_mechanism_and_target_with_merged_evidence() -> None:
    registry = new_registry()
    first = record(
        registry,
        "get_service_logs",
        logs("checkout", (-2, "ERROR", upstream("payment"))),
        service="checkout",
    )
    second = record(
        registry,
        "get_service_logs",
        logs("checkout", (-1, "ERROR", upstream("payment"))),
        service="checkout",
        limit=100,
    )
    scoped = record(
        registry, "get_k8s_like_events", events(("payment", "OOMKilled", -2)), service="payment"
    )
    cluster = record(
        registry, "get_k8s_like_events", events(("payment", "OOMKilled", -2)), limit=30
    )

    hypotheses = generate_hypotheses(registry.all(), make_incident())

    [dependency] = by_mechanism(hypotheses, M.DEPENDENCY_FAILURE)
    assert dependency.supporting_evidence_ids == [first.id, second.id]
    [pressure] = by_mechanism(hypotheses, M.RESOURCE_PRESSURE)
    assert pressure.supporting_evidence_ids == [scoped.id, cluster.id]


# --- H-memory -------------------------------------------------------------------------------


def test_memory_hint_without_live_support_cites_nothing() -> None:
    [hypothesis] = generate_hypotheses([], make_incident(), [memory_hit()])

    assert hypothesis.origin == "memory"
    assert (hypothesis.mechanism, hypothesis.target_service) == (M.BAD_DEPLOYMENT, "checkout")
    assert hypothesis.claim == (
        "Past verified episode ep-1 suggests bad deployment on checkout; "
        "no live evidence shows it yet"
    )
    assert hypothesis.supporting_evidence_ids == []
    assert hypothesis.details == {"episode_id": "ep-1"}


def test_memory_hint_that_live_evidence_confirms_borrows_its_citations() -> None:
    registry = new_registry()
    record(registry, "get_recent_deployments", releases(("checkout", "v2.4.1", -12, "v2.4.0")))
    record(registry, "get_service_logs", logs("checkout", (-5, "ERROR", NPE)), service="checkout")

    live, remembered = generate_hypotheses(registry.all(), make_incident(), [memory_hit()])

    assert (live.origin, remembered.origin) == ("investigation", "memory")  # live first
    assert remembered.supporting_evidence_ids == live.supporting_evidence_ids
    assert remembered.details == {**live.details, "episode_id": "ep-1"}
    assert remembered.claim.endswith("live evidence shows the same pattern")


def test_memory_target_is_the_episode_service_only_if_this_incident_involves_it() -> None:
    hits = [memory_hit(target="payment")]
    [involved] = generate_hypotheses([], make_incident("checkout", ["checkout", "payment"]), hits)
    assert involved.target_service == "payment"

    [elsewhere] = generate_hypotheses([], make_incident("frontend"), hits)
    assert elsewhere.target_service == "frontend"  # the alerted service


def test_only_verified_successes_with_a_known_mechanism_become_memory_hypotheses() -> None:
    hints = [
        memory_hit(outcome=EpisodeOutcome.FAILED, episode_id="ep-failed"),
        memory_hit(mechanism=None, episode_id="ep-undiagnosed"),
        memory_hit(mechanism=M.UNKNOWN, episode_id="ep-unknown"),
    ]
    assert generate_hypotheses([], make_incident(), hints) == []

    # Two episodes with the same suggestion give one hypothesis, from the most similar one.
    [hypothesis] = generate_hypotheses(
        [], make_incident(), [memory_hit(episode_id="ep-a"), memory_hit(episode_id="ep-b")]
    )
    assert hypothesis.details["episode_id"] == "ep-a"


# --- properties of the whole output ---------------------------------------------------------


def mixed_incident_evidence() -> EvidenceRegistry:
    """Errors on checkout caused by a crash-looping payment, plus a recent checkout release."""
    registry = new_registry()
    record(
        registry,
        "get_recent_deployments",
        releases(("checkout", "v2.4.1", -40, "v2.4.0")),
        limit=10,
    )
    record(registry, "get_k8s_like_events", events(("payment", "OOMKilled", -3)), limit=30)
    record(
        registry,
        "get_service_logs",
        logs("checkout", (-2, "ERROR", upstream("payment")), (-1, "ERROR", upstream("inventory"))),
        service="checkout",
    )
    record(registry, "get_service_health", health("checkout", error_rate=0.4), service="checkout")
    return registry


def test_cited_ids_are_real_unique_and_the_order_is_deterministic() -> None:
    registry = mixed_incident_evidence()
    hints = [memory_hit()]

    hypotheses = generate_hypotheses(registry.all(), make_incident(), hints)

    def keys(found: list) -> list[tuple]:
        return [
            (h.mechanism, h.target_service, h.origin, h.details.get("dependency")) for h in found
        ]

    # Same input, same output; and the order never depends on the order evidence arrived in.
    assert hypotheses == generate_hypotheses(registry.all(), make_incident(), hints)
    reordered = generate_hypotheses(list(reversed(registry.all())), make_incident(), hints)
    assert keys(reordered) == keys(hypotheses)
    assert keys(hypotheses) == [
        (M.BAD_DEPLOYMENT, "checkout", "investigation", None),
        (M.BAD_DEPLOYMENT, "checkout", "memory", None),
        (M.RESOURCE_PRESSURE, "payment", "investigation", None),
        (M.DEPENDENCY_FAILURE, "checkout", "investigation", "inventory"),
        (M.DEPENDENCY_FAILURE, "checkout", "investigation", "payment"),
    ]
    known = set(registry.ids())
    assert all(h.supporting_evidence_ids for h in hypotheses)
    assert all(set(h.supporting_evidence_ids) <= known for h in hypotheses)


def test_payloads_that_do_not_fit_their_model_are_skipped() -> None:
    registry = new_registry()
    registry.add(
        source_type=SourceType.TOOL,
        source_ref="mcp:get_service_logs(service=checkout)",
        content="garbage",
        data={"lines": "not a list"},
        tool_name="get_service_logs",
    )
    registry.add(source_type=SourceType.DOC, source_ref="doc:runbook", content="text")
    # An extra bookkeeping key does not hide an otherwise valid observation.
    valid = health("checkout", error_rate=0.3).model_dump(mode="json") | {"note": "extra"}
    registry.add(
        source_type=SourceType.TOOL,
        source_ref="mcp:get_service_health(service=checkout)",
        content="health",
        data=valid,
        tool_name="get_service_health",
    )

    parsed = parse_evidence(registry.all())

    assert parsed.logs == [] and len(parsed.health) == 1
    assert generate_hypotheses(registry.all(), make_incident()) == []
