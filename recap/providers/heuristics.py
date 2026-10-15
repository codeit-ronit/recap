"""Hypothesis heuristics of the scripted baseline policy (NOT an LLM).

Where it sits:

    tool evidence --> [heuristics] --> naive Hypotheses --> EvidenceVerifier --> best diagnosis

The scripted policy cannot read free text the way a language model can. Instead it parses
the structured tool outputs stored in the evidence registry (the observation models of
recap/core/observations.py) and matches the simulator's signal conventions
(recap/core/signals.py) to propose root-cause hypotheses:

    H-deploy      a release younger than RECENT_DEPLOY_MINUTES + any error signal on S
                  -> bad_deployment(S)
    H-resource    OOMKilled/BackOff events, memory near the limit or OOM log lines on S
                  -> resource_pressure(S)
    H-dependency  S logs "upstream D call failed" errors -> dependency_failure(S -> D)
    H-memory      a verified, successful past episode -> its mechanism (origin "memory")

Every hypothesis cites the IDs of the evidence it came from. The rules are deliberately
naive and recall-oriented: proposing a wrong hypothesis is cheap, missing the right one is
not. Deciding which hypothesis the evidence really supports is the job of the trusted
EvidenceVerifier, never of this module. A real cluster would need its own parsers (or an
LLM provider that reads raw text), because these rules only know the simulator's formats.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from recap.core.models import (
    EpisodeOutcome,
    Evidence,
    FailureMechanism,
    Hypothesis,
    Incident,
    MemoryHit,
)
from recap.core.observations import (
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    HealthSnapshot,
    LogQueryResult,
    MetricSeries,
    RollbackPlan,
)
from recap.core.signals import (
    ERROR_RATE_THRESHOLD,
    MEMORY_PRESSURE_RATIO,
    OOM_EVENT_REASONS,
    RECENT_DEPLOY_MINUTES,
    as_utc,
    is_oom_message,
    minutes_between,
    upstream_dependency,
)

Model = TypeVar("Model", bound=BaseModel)

# The observation model each read tool returns; its Evidence.data is that model, dumped.
TOOL_MODELS: dict[str, type[BaseModel]] = {
    "get_recent_deployments": DeploymentList,
    "get_service_logs": LogQueryResult,
    "get_service_metrics": MetricSeries,
    "get_k8s_like_events": EventList,
    "get_service_health": HealthSnapshot,
    "propose_rollback": RollbackPlan,
}

# Hypotheses are sorted by mechanism in the enum's order (actionable mechanisms first).
_MECHANISM_RANK = {mechanism: rank for rank, mechanism in enumerate(FailureMechanism)}


@dataclass
class ParsedEvidence:
    """Tool evidence turned back into observation models, each paired with its evidence ID."""

    deployments: list[tuple[str, DeploymentRecord]] = field(default_factory=list)
    logs: list[tuple[str, LogQueryResult]] = field(default_factory=list)
    metrics: list[tuple[str, MetricSeries]] = field(default_factory=list)
    events: list[tuple[str, ClusterEvent]] = field(default_factory=list)
    health: list[tuple[str, HealthSnapshot]] = field(default_factory=list)
    rollback_plans: list[tuple[str, RollbackPlan]] = field(default_factory=list)

    def services(self) -> list[str]:
        """Every service the evidence mentions, sorted so rule output is deterministic."""
        names = {record.service for _, record in self.deployments}
        names |= {logs.service for _, logs in self.logs}
        names |= {series.service for _, series in self.metrics}
        names |= {event.service for _, event in self.events}
        names |= {snapshot.service for _, snapshot in self.health}
        return sorted(names)

    def reference_time(self, alert_time: datetime) -> datetime:
        """'Now' for the rules: the newest timestamp in the observations, never before the alert.

        Evidence.timestamp is the wall-clock moment a result was recorded, while observations
        carry environment (simulated) time, so "now" comes from the data. The alert time is a
        floor: when only old deployment records are in hand, the newest of them is not "now".
        """
        # Observation timestamps are UTC once parsed; the alert time may still be naive.
        stamps = [as_utc(alert_time), *(record.deployed_at for _, record in self.deployments)]
        stamps += [line.timestamp for _, logs in self.logs for line in logs.lines]
        stamps += [point.timestamp for _, series in self.metrics for point in series.points]
        stamps += [event.timestamp for _, event in self.events]
        stamps += [snapshot.observed_at for _, snapshot in self.health]
        return max(stamps)

    def latest_deployment(self, service: str) -> tuple[str, DeploymentRecord] | None:
        """The newest deployment of `service`, with the ID of the evidence that shows it."""
        records = [pair for pair in self.deployments if pair[1].service == service]
        return max(records, key=lambda pair: pair[1].deployed_at, default=None)

    def recent_deployment(self, service: str, now: datetime) -> tuple[str, DeploymentRecord] | None:
        """The newest deployment of `service` if it is younger than RECENT_DEPLOY_MINUTES."""
        latest = self.latest_deployment(service)
        if latest is None:
            return None
        age = minutes_between(latest[1].deployed_at, now)
        return latest if age < RECENT_DEPLOY_MINUTES else None

    def rollback_plan(self, service: str) -> tuple[str, RollbackPlan] | None:
        """The most recent rollback dry run for `service`, with its evidence ID."""
        plans = [pair for pair in self.rollback_plans if pair[1].service == service]
        return plans[-1] if plans else None

    def error_evidence(self, service: str) -> list[str]:
        """IDs of evidence showing `service` failing: ERROR logs or an error rate over threshold."""
        ids = [eid for eid, logs in self.logs if logs.service == service and _has_errors(logs)]
        ids += [eid for eid, s in self.metrics if s.service == service and _over_threshold(s)]
        ids += [
            eid
            for eid, snapshot in self.health
            if snapshot.service == service and snapshot.error_rate > ERROR_RATE_THRESHOLD
        ]
        return list(dict.fromkeys(ids))

    def memory_pressure(self, service: str) -> dict[str, list[str]]:
        """Evidence IDs per memory-pressure signal of `service` (signals not seen are left out)."""
        signals = {
            "OOMKilled/BackOff events": [
                eid
                for eid, event in self.events
                if event.service == service and event.reason in OOM_EVENT_REASONS
            ],
            f"memory at or above {MEMORY_PRESSURE_RATIO:.0%} of its limit": [
                eid
                for eid, series in self.metrics
                if series.service == service and _near_limit(series)
            ],
            "out-of-memory errors in its logs": [
                eid for eid, logs in self.logs if logs.service == service and _has_oom_lines(logs)
            ],
        }
        return {signal: list(dict.fromkeys(ids)) for signal, ids in signals.items() if ids}

    def upstream_failures(self, service: str) -> dict[str, list[str]]:
        """Dependencies named in `service`'s 'upstream X call failed' ERROR lines -> evidence IDs.

        Dependencies keep the order in which they first appear, so callers can follow them.
        """
        found: dict[str, list[str]] = {}
        for evidence_id, logs in self.logs:
            if logs.service != service:
                continue
            for line in logs.lines:
                dependency = upstream_dependency(line.message)
                # Only ERROR lines count as failures; a service never "depends on" itself.
                if line.level == "ERROR" and dependency and dependency != service:
                    found.setdefault(dependency, []).append(evidence_id)
        return {dependency: list(dict.fromkeys(ids)) for dependency, ids in found.items()}


def parse_evidence(evidence: Iterable[Evidence]) -> ParsedEvidence:
    """Turn tool evidence back into observation models; payloads that do not fit are skipped."""
    parsed = ParsedEvidence()
    for item in evidence:
        model = TOOL_MODELS.get(item.tool_name or "")
        observation = _validate(model, item.data) if model else None
        if isinstance(observation, DeploymentList):
            parsed.deployments += [(item.id, record) for record in observation.deployments]
        elif isinstance(observation, LogQueryResult):
            parsed.logs.append((item.id, observation))
        elif isinstance(observation, MetricSeries):
            parsed.metrics.append((item.id, observation))
        elif isinstance(observation, EventList):
            parsed.events += [(item.id, event) for event in observation.events]
        elif isinstance(observation, HealthSnapshot):
            parsed.health.append((item.id, observation))
        elif isinstance(observation, RollbackPlan):
            parsed.rollback_plans.append((item.id, observation))
    return parsed


def generate_hypotheses(
    evidence: Sequence[Evidence], incident: Incident, memory_hints: Sequence[MemoryHit] = ()
) -> list[Hypothesis]:
    """Naive root-cause hypotheses from tool evidence and past episodes, each citing evidence IDs.

    Ordered by mechanism, then target (then live evidence before memory), so the same
    evidence always gives the same list.
    """
    parsed = parse_evidence(evidence)
    now = parsed.reference_time(incident.created_at)
    live = [
        *_deployment_rule(parsed, now, incident.created_at),
        *_resource_rule(parsed),
        *_dependency_rule(parsed),
    ]
    remembered = _memory_rule(live, incident, memory_hints)
    return sorted([*live, *remembered], key=_order)


def memory_target(hit: MemoryHit, incident: Incident) -> str:
    """Where a remembered diagnosis applies today: its service if this incident involves it."""
    remembered = hit.episode.mechanism_target
    if remembered and remembered in (incident.service, *incident.affected_services):
        return remembered
    return incident.service


# --- the four rules ------------------------------------------------------------------------


def _deployment_rule(
    parsed: ParsedEvidence, now: datetime, alert_time: datetime
) -> list[Hypothesis]:
    """H-deploy: a recent release on a service that shows any error signal."""
    found: list[Hypothesis] = []
    for service in parsed.services():
        recent = parsed.recent_deployment(service, now)
        errors = parsed.error_evidence(service)
        if recent is None or not errors:
            continue
        evidence_id, release = recent
        when = _relative(release.deployed_at, alert_time)
        found.append(
            Hypothesis(
                claim=(
                    f"{service} {release.version}, deployed {when}, may have introduced the errors"
                ),
                mechanism=FailureMechanism.BAD_DEPLOYMENT,
                target_service=service,
                supporting_evidence_ids=list(dict.fromkeys([evidence_id, *errors])),
                details={
                    "version": release.version,
                    "deployed_at": release.deployed_at.isoformat(),
                },
            )
        )
    return found


def _resource_rule(parsed: ParsedEvidence) -> list[Hypothesis]:
    """H-resource: OOM kills, memory near the limit or out-of-memory errors on a service."""
    pressure = {service: parsed.memory_pressure(service) for service in parsed.services()}
    return [
        Hypothesis(
            claim=f"{service} may be under memory pressure: {', '.join(signals)}",
            mechanism=FailureMechanism.RESOURCE_PRESSURE,
            target_service=service,
            supporting_evidence_ids=list(dict.fromkeys(e for ids in signals.values() for e in ids)),
        )
        for service, signals in pressure.items()
        if signals
    ]


def _dependency_rule(parsed: ParsedEvidence) -> list[Hypothesis]:
    """H-dependency: a service whose logs say its calls to a dependency fail."""
    return [
        Hypothesis(
            claim=(
                f"{service} errors may come from its dependency {dependency} "
                f"(its logs show 'upstream {dependency} call failed')"
            ),
            mechanism=FailureMechanism.DEPENDENCY_FAILURE,
            target_service=service,
            supporting_evidence_ids=ids,
            details={"dependency": dependency},
        )
        for service in parsed.services()
        for dependency, ids in parsed.upstream_failures(service).items()
    ]


def _memory_rule(
    live: list[Hypothesis], incident: Incident, hints: Sequence[MemoryHit]
) -> list[Hypothesis]:
    """H-memory: what verified past episodes suggest, cited with the live evidence that agrees.

    A remembered diagnosis has no evidence of its own in this run. It borrows the citations
    of a live hypothesis with the same mechanism and target, or cites nothing when live
    evidence does not show it, so the verifier gives it no free pass.
    """
    agreeing = {(h.mechanism, h.target_service): h for h in live}
    found: dict[tuple[FailureMechanism, str], Hypothesis] = {}
    for hit in hints:
        episode = hit.episode
        mechanism = episode.failure_mechanism
        if hit.outcome != EpisodeOutcome.SUCCESS or not episode.verified:
            continue  # only verified recoveries are worth suggesting again
        if mechanism is None or mechanism == FailureMechanism.UNKNOWN:
            continue
        target = memory_target(hit, incident)
        if (mechanism, target) in found:
            continue  # hits arrive most similar first; one suggestion per mechanism and target
        match = agreeing.get((mechanism, target))
        verdict = (
            "live evidence shows the same pattern" if match else "no live evidence shows it yet"
        )
        found[(mechanism, target)] = Hypothesis(
            claim=(
                f"Past verified episode {episode.episode_id} suggests "
                f"{mechanism.value.replace('_', ' ')} on {target}; {verdict}"
            ),
            mechanism=mechanism,
            target_service=target,
            supporting_evidence_ids=list(match.supporting_evidence_ids) if match else [],
            details={**(match.details if match else {}), "episode_id": episode.episode_id},
            origin="memory",
        )
    return list(found.values())


# --- small helpers -------------------------------------------------------------------------


def _validate(model: type[Model], data: dict[str, Any]) -> Model | None:
    """Check one tool payload against its observation model; None if it does not fit."""
    # Tool output is untrusted data: keys the model does not define are ignored, and a
    # payload that still does not fit is skipped instead of crashing the agent.
    known = {key: value for key, value in data.items() if key in model.model_fields}
    try:
        return model.model_validate(known)
    except ValidationError:
        return None


def _has_errors(logs: LogQueryResult) -> bool:
    """True if any log line has level ERROR."""
    return any(line.level == "ERROR" for line in logs.lines)


def _has_oom_lines(logs: LogQueryResult) -> bool:
    """True if any log line reports an out-of-memory condition."""
    return any(is_oom_message(line.message) for line in logs.lines)


def _over_threshold(series: MetricSeries) -> bool:
    """True if an error_rate series goes above ERROR_RATE_THRESHOLD at any point."""
    return series.metric == "error_rate" and any(
        point.value > ERROR_RATE_THRESHOLD for point in series.points
    )


def _near_limit(series: MetricSeries) -> bool:
    """True if a memory_mb series reaches MEMORY_PRESSURE_RATIO of its container limit."""
    if series.metric != "memory_mb" or not series.limit:
        return False
    return any(point.value >= MEMORY_PRESSURE_RATIO * series.limit for point in series.points)


def _relative(moment: datetime, alert_time: datetime) -> str:
    """Plain-English offset from the alert, e.g. '12 min before the alert'."""
    minutes = round(minutes_between(moment, alert_time))
    return f"{minutes} min before the alert" if minutes >= 0 else f"{-minutes} min after the alert"


def _order(hypothesis: Hypothesis) -> tuple[int, str, bool, str]:
    """Sort key: mechanism, then target, then live evidence before memory."""
    return (
        _MECHANISM_RANK[hypothesis.mechanism],
        hypothesis.target_service or "",
        hypothesis.origin == "memory",
        str(hypothesis.details.get("dependency", "")),
    )
