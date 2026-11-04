"""What a target environment returns when RECAP observes or changes it.

These models are the *adapter contract*: the simulator produces them, and the Kubernetes
adapter (the Kind demo and SREGym's cluster, M5) translates real cluster data into the
same shapes. Because agents, the verifier and the oracle only ever see these models,
swapping the infrastructure does not require touching any of them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field

from recap.core.models import Contract
from recap.core.signals import as_utc

# Adapters may return timestamps with or without a timezone; RECAP stores them all in UTC.
UtcDatetime = Annotated[datetime, AfterValidator(as_utc)]

MetricName = Literal["error_rate", "latency_p95_ms", "memory_mb", "cpu_pct", "restarts"]
ServiceStatus = Literal["Running", "CrashLoopBackOff", "Deleted", "Unknown"]
EnvironmentAction = Literal["rollback", "restart", "delete_namespace"]


class AlertSignal(Contract):
    """The alert produced when a scenario/fault is reset. It opens a new incident."""

    service: str
    affected_services: list[str] = Field(default_factory=list)
    symptoms: list[str]
    namespace: str
    fired_at: UtcDatetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ServiceInfo(Contract):
    name: str
    namespace: str
    version: str
    replicas: int
    dependencies: list[str] = Field(default_factory=list)  # services this one calls


class ServiceList(Contract):
    namespace: str
    services: list[ServiceInfo]


class HealthSnapshot(Contract):
    """Point-in-time health as an operator dashboard would show it (noisy, interpretable)."""

    service: str
    status: ServiceStatus
    ready_replicas: int
    replicas: int
    # Fraction of failing requests over the last 5 minutes (0..1), and the p95 latency. None:
    # not measured, on a target that exports no request metrics (SREGym's hotel reservation):
    # status and readiness are then all the health there is, and nothing is guessed.
    error_rate: float | None
    latency_p95_ms: float | None
    observed_at: UtcDatetime


class LogLine(Contract):
    timestamp: UtcDatetime
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"]
    service: str
    message: str


class LogQueryResult(Contract):
    service: str
    lines: list[LogLine]  # chronological, newest last
    total_matched: int
    truncated: bool = False


class MetricPoint(Contract):
    timestamp: UtcDatetime
    value: float


class MetricSeries(Contract):
    service: str
    metric: MetricName
    unit: str
    points: list[MetricPoint]  # chronological, one point per step
    step_minutes: int = 5
    limit: float | None = None  # e.g. the container memory limit for memory_mb


class DeploymentRecord(Contract):
    service: str
    version: str
    previous_version: str | None = None
    # The ReplicaSet that introduced this release (Kubernetes), and those of its later restarts
    # (same image, new pod template), so a pod's events can be tied to the release they belong
    # to; None and [] where the target has no such thing (the simulator).
    replicaset: str | None = None
    restart_replicasets: list[str] = Field(default_factory=list)
    deployed_at: UtcDatetime
    status: Literal["complete", "rollback"] = "complete"
    change_summary: str = ""


class DeploymentList(Contract):
    deployments: list[DeploymentRecord]  # newest first


class ClusterEvent(Contract):
    """A Kubernetes-like event (OOMKilled, BackOff, ScalingReplicaSet, ...)."""

    timestamp: UtcDatetime
    type: Literal["Normal", "Warning"]
    reason: str
    object: str  # e.g. "pod/payment-5d8f7-x2k9q"
    service: str
    message: str
    count: int = 1
    # When it first happened: an aggregated event (a crash loop's "Back-off ... x20") began
    # long before `timestamp`, its latest occurrence. None: the source does not report it.
    first_seen: UtcDatetime | None = None


class EventList(Contract):
    events: list[ClusterEvent]  # newest first


class RollbackPlan(Contract):
    """Dry-run answer to 'what would a rollback do?'. Has no side effects."""

    service: str
    current_version: str
    target_version: str | None = None
    possible: bool
    reason: str


class AuthorizedAction(Contract):
    """A write that RECAP's central safety gateway allowed, on its way to the environment.

    `capability` is the proof: a signed, short-lived, single-use token for exactly this
    action, which the environment verifies before it changes anything (see
    recap/safety/capabilities.py). Without a valid one the write is refused, whoever sent
    it. `approval_id` and `proposal_digest` name the approved proposal for the audit trail
    (the capability binds them too).
    """

    action: EnvironmentAction
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    approval_id: str = ""
    proposal_digest: str = ""
    capability: str = ""  # never logged or stored: it is a bearer token until it is used


class ActionOutcome(Contract):
    ok: bool
    action: str
    target: str
    message: str
    side_effects: list[str] = Field(default_factory=list)
    executed_at: UtcDatetime


class GroundTruthHealth(Contract):
    """Oracle-only health of one service, read directly from environment state."""

    service: str
    status: ServiceStatus
    ready_replicas: int
    replicas: int
    error_rate: float | None  # without observation noise; None: the oracle has no probe here


class HealthReport(Contract):
    checked_at: UtcDatetime
    services: dict[str, GroundTruthHealth]
