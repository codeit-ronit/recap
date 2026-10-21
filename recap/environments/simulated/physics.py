"""Simulator physics: pure functions that turn the sandbox state into observations.

The simulated world is a plain dict (`state`) that the engine builds from a scenario file
and stores in SQLite. Nothing here does I/O or reads the wall clock: every observation is
a pure function of (state, minute), and all noise comes from a random generator seeded
with (seed, service, minute, kind). Same scenario + same seed = exactly the same world.

Rules of the world (minute 0 is the alert time; executed writes move the clock forward):
  * bad_release  The service errors and is slow while it runs the bad version. A restart
                 does not help; a rollback to any other version does.
  * memory_leak  Memory climbs from baseline to the container limit, then the pod is
                 OOM-killed every few minutes (CrashLoopBackOff, one replica not ready).
                 A restart or a rollback replaces the pods and clears the leak.
  * propagation  A caller of an erroring dependency inherits 80% of its error rate and logs
                 "upstream <dep> call failed", while its own pods stay Running and Ready.
  * deletion     After delete_namespace every service is Deleted and nothing new happens.

Log and event texts use the templates in recap/core/signals.py, so the scripted policy and
the evidence verifier can parse exactly what the simulator writes.
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from graphlib import TopologicalSorter
from typing import Any

from recap.core.ids import sha256_hex
from recap.core.observations import (
    ActionOutcome,
    AuthorizedAction,
    ClusterEvent,
    GroundTruthHealth,
    HealthReport,
    HealthSnapshot,
    LogLine,
    MetricPoint,
    MetricSeries,
    ServiceStatus,
)
from recap.core.signals import (
    ERROR_RATE_THRESHOLD,
    HEAP_WARNING_TEMPLATE,
    OOM_ERROR_MESSAGE,
    RELEASE_ERROR_TEMPLATE,
    RPC_HEAP_WARNING_TEMPLATE,
    RPC_OOM_ERROR_MESSAGE,
    RPC_RELEASE_ERROR_TEMPLATE,
    RPC_UPSTREAM_ERROR_TEMPLATE,
    UPSTREAM_ERROR_TEMPLATE,
)

State = dict[str, Any]
Fault = dict[str, Any]

# How a service words its messages, by the scenario's log_style: (heap warning, OOM error,
# release error, upstream error). The verifier and the scripted policy parse both styles.
_STYLES = {
    "java": (
        HEAP_WARNING_TEMPLATE,
        OOM_ERROR_MESSAGE,
        RELEASE_ERROR_TEMPLATE,
        UPSTREAM_ERROR_TEMPLATE,
    ),
    "rpc": (
        RPC_HEAP_WARNING_TEMPLATE,
        RPC_OOM_ERROR_MESSAGE,
        RPC_RELEASE_ERROR_TEMPLATE,
        RPC_UPSTREAM_ERROR_TEMPLATE,
    ),
}

# --- Rules of the simulated world -------------------------------------------------------

PROPAGATION_FACTOR = 0.8  # a caller inherits 80% of a failing dependency's error rate,
PROPAGATION_CAP = 0.95  # but inherited errors never push it above 95%
# Calls to a failing dependency time out after 3 s. Once more than 5% of a caller's requests
# wait for that timeout, the caller's p95 latency is about the timeout itself.
UPSTREAM_TIMEOUT_MS = 3000
UPSTREAM_REASON = f"timeout after {UPSTREAM_TIMEOUT_MS}ms"
HEAP_WARNING_RATIO = 0.80  # services warn about heap usage above 80% of their limit
CRASH_CYCLE_MINUTES = 3  # an OOM-killed pod comes back and dies again every 3 minutes
METRIC_STEP_MINUTES = 5
EVENT_RETENTION_MINUTES = 60  # like Kubernetes, events older than about an hour are gone
CLOCK_STEP = {"rollback": 5, "restart": 5, "delete_namespace": 1}  # minutes an action takes
METRIC_UNITS = {
    "error_rate": "ratio",
    "latency_p95_ms": "ms",
    "memory_mb": "MB",
    "cpu_pct": "percent",
    "restarts": "count",
}
ENDPOINTS = {  # used in the normal "GET /api/<service>/<endpoint> 200" log lines
    "frontend": "home",
    "checkout": "orders",
    "payment": "charges",
    "inventory": "stock",
    "cart": "items",
}
RESTART_EVENTS = [("Killing", "Stopping container {name}"), ("Started", "Started container {name}")]
CRASH_EVENTS = [
    ("OOMKilled", "Container {name} exceeded its memory limit ({limit}Mi) and was killed"),
    ("BackOff", "Back-off restarting failed container {name}"),
    ("Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 503"),
]


# --- Time, versions and faults ------------------------------------------------------------


def timestamp(state: State, minute: int) -> datetime:
    """Wall-clock time of a simulated minute (minute 0 is the scenario's incident time)."""
    return datetime.fromisoformat(state["incident_time"]) + timedelta(minutes=minute)


def namespace_deleted(state: State, minute: int) -> bool:
    """True once delete_namespace has run."""
    deleted_at = state["namespace_deleted_at"]
    return deleted_at is not None and minute >= deleted_at


def history(state: State, service: str, until: int | None = None) -> list[dict[str, Any]]:
    """Deployment records of one service, oldest first (optionally only up to `until`)."""
    return [
        record
        for record in state["deployments"]
        if record["service"] == service and (until is None or record["minute"] <= until)
    ]


def version_at(state: State, service: str, minute: int) -> str:
    """The version a service runs at `minute`: its latest deployment at or before then."""
    # Before the first recorded deployment, assume the oldest known version was running.
    deployed = history(state, service, until=minute) or history(state, service)[:1]
    return deployed[-1]["version"]


def rollback_target(state: State, service: str, minute: int) -> str | None:
    """The most recent earlier version in the history that differs from the running one."""
    current = version_at(state, service, minute)
    past = [record["version"] for record in history(state, service, until=minute)]
    return next((version for version in reversed(past) if version != current), None)


def active_release(state: State, service: str, minute: int) -> Fault | None:
    """The bad_release hurting `service` at `minute` (only while the bad version runs)."""
    for fault in _faults(state, "bad_release", service):
        started = minute >= -fault["started_minutes_ago"]
        if started and version_at(state, service, minute) == fault["version"]:
            return fault
    return None


def active_leak(state: State, service: str, minute: int) -> Fault | None:
    """The memory_leak of `service` if it has started and its pods were not replaced yet."""
    for fault in _faults(state, "memory_leak", service):
        cleared = fault["cleared_at"] is not None and minute >= fault["cleared_at"]
        if minute >= -fault["started_minutes_ago"] and not cleared:
            return fault
    return None


def crash_minutes(leak: Fault, until: int) -> range:
    """Minutes at which the leaking pod was OOM-killed, from the first OOM up to `until`."""
    first = -leak["oom_minutes_ago"]
    last = until if leak["cleared_at"] is None else min(until, leak["cleared_at"] - 1)
    return range(first, last + 1, CRASH_CYCLE_MINUTES)


def memory_truth(service: dict[str, Any], leak: Fault | None, minute: int) -> float:
    """Noise-free memory: baseline, then a straight climb to the limit, then pinned there."""
    base, limit = service["baseline_memory_mb"], service["memory_limit_mb"]
    if leak is None:
        return float(base)
    start, oom = -leak["started_minutes_ago"], -leak["oom_minutes_ago"]
    if minute >= oom:
        return float(limit)
    return base + (limit - base) * (minute - start) / (oom - start)


def _faults(state: State, kind: str, service: str) -> list[Fault]:
    return [f for f in state["faults"] if f["type"] == kind and f["service"] == service]


# --- Ground truth, computed dependencies first so errors flow to callers -----------------


@dataclass(frozen=True)
class Condition:
    """Noise-free truth about one service at one minute."""

    status: ServiceStatus
    ready_replicas: int
    replicas: int
    error_rate: float
    latency_ms: float
    release: Fault | None = None  # the active bad_release, if any
    leak: Fault | None = None  # the active memory_leak, if any
    oom: bool = False  # the leak has hit the memory limit (crash loop)
    failing_dependencies: tuple[str, ...] = ()


def conditions(state: State, minute: int) -> dict[str, Condition]:
    """Truth for every service at `minute` (callers see their dependencies' failures)."""
    graph = {name: service["dependencies"] for name, service in state["services"].items()}
    truth: dict[str, Condition] = {}
    for name in TopologicalSorter(graph).static_order():  # dependencies come first
        truth[name] = _condition(state, name, minute, truth)
    return truth


def _condition(state: State, name: str, minute: int, truth: dict[str, Condition]) -> Condition:
    service = state["services"][name]
    replicas = service["replicas"]
    if namespace_deleted(state, minute):
        return Condition(
            status="Deleted", ready_replicas=0, replicas=replicas, error_rate=1.0, latency_ms=0.0
        )
    release = active_release(state, name, minute)
    leak = active_leak(state, name, minute)
    oom = leak is not None and minute >= -leak["oom_minutes_ago"]
    error = (release["error_rate"] if release else 0.0) + (leak["error_rate"] if oom else 0.0)
    latency = float(release["latency_ms"] if release else service["base_latency_ms"])
    failing = tuple(
        d for d in service["dependencies"] if truth[d].error_rate > ERROR_RATE_THRESHOLD
    )
    for dep in failing:
        inherited = min(PROPAGATION_CAP, error + PROPAGATION_FACTOR * truth[dep].error_rate)
        error = max(error, inherited)  # inheriting never lowers a service's own errors
        latency = max(latency, UPSTREAM_TIMEOUT_MS + service["base_latency_ms"])
    return Condition(
        status="CrashLoopBackOff" if oom else "Running",
        ready_replicas=replicas - 1 if oom else replicas,  # the crash-looping pod is not ready
        replicas=replicas,
        error_rate=round(min(1.0, error), 4),
        latency_ms=latency,
        release=release,
        leak=leak,
        oom=oom,
        failing_dependencies=failing,
    )


def ground_truth(state: State, minute: int) -> HealthReport:
    """Noise-free health of every service: what the recovery oracle reads."""
    truth = conditions(state, minute)
    services = {
        name: GroundTruthHealth(
            service=name,
            status=truth[name].status,
            ready_replicas=truth[name].ready_replicas,
            replicas=truth[name].replicas,
            error_rate=truth[name].error_rate,
        )
        for name in state["services"]
    }
    return HealthReport(checked_at=timestamp(state, minute), services=services)


# --- What operators see: noisy health and metrics ------------------------------------------


def health_snapshot(state: State, name: str, minute: int) -> HealthSnapshot:
    """Point-in-time health as a dashboard shows it (with a little measurement noise)."""
    cond = conditions(state, minute)[name]
    return HealthSnapshot(
        service=name,
        status=cond.status,
        ready_replicas=cond.ready_replicas,
        replicas=cond.replicas,
        error_rate=_reading(state, name, "error_rate", minute, cond),
        latency_p95_ms=_reading(state, name, "latency_p95_ms", minute, cond),
        observed_at=timestamp(state, minute),
    )


def metric_series(state: State, name: str, metric: str, window: int, now: int) -> MetricSeries:
    """`metric` every 5 minutes from `now - window` up to and including `now`."""
    if metric not in METRIC_UNITS:
        raise ValueError(f"unknown metric '{metric}'")
    minutes = [now - METRIC_STEP_MINUTES * k for k in range(window // METRIC_STEP_MINUTES, -1, -1)]
    points = []
    for minute in minutes:
        value = _reading(state, name, metric, minute, conditions(state, minute)[name])
        points.append(MetricPoint(timestamp=timestamp(state, minute), value=value))
    limit = state["services"][name]["memory_limit_mb"] if metric == "memory_mb" else None
    unit = METRIC_UNITS[metric]
    return MetricSeries(service=name, metric=metric, unit=unit, points=points, limit=limit)


def restart_count(state: State, name: str, minute: int) -> int:
    """Container restarts so far: crash-loop restarts plus restarts ordered by an operator."""
    ordered = sum(1 for r in state["restarts"] if r["service"] == name and r["minute"] <= minute)
    crashed = sum(len(crash_minutes(leak, minute)) for leak in _faults(state, "memory_leak", name))
    return ordered + crashed


def _reading(state: State, name: str, metric: str, minute: int, cond: Condition) -> float:
    """One measurement of `metric`: the truth plus small seeded jitter."""
    if cond.status == "Deleted":
        return 1.0 if metric == "error_rate" else 0.0  # nothing is left to measure
    service = state["services"][name]
    # One generator per (seed, service, minute, metric): readings never depend on call order.
    rng = random.Random(f"{state['seed']}|{name}|{minute}|{metric}")
    if metric == "error_rate":
        return round(max(0.0, min(1.0, cond.error_rate + rng.uniform(-0.003, 0.003))), 4)
    if metric == "latency_p95_ms":
        return round(cond.latency_ms + rng.uniform(-10, 10), 1)
    if metric == "cpu_pct":
        return round(service["base_cpu_pct"] + rng.uniform(-5, 5), 1)
    if metric == "memory_mb":
        if cond.oom:  # hovers at 97-100% of the limit while the pod keeps being killed
            return round(service["memory_limit_mb"] * rng.uniform(0.97, 1.0), 1)
        return round(memory_truth(service, cond.leak, minute), 1)
    return float(restart_count(state, name, minute))


# --- Logs and events ------------------------------------------------------------------------


def log_lines(state: State, name: str, start: int, end: int) -> list[LogLine]:
    """Every log line of a service from minute `start` to `end` (inclusive), oldest first."""
    return [
        LogLine(timestamp=timestamp(state, minute), level=level, service=name, message=message)
        for minute in range(start, end + 1)
        for level, message in _messages_at(state, name, minute)
    ]


def _messages_at(state: State, name: str, minute: int) -> list[tuple[str, str]]:
    cond = conditions(state, minute)[name]
    if cond.status == "Deleted":
        return []  # a deleted service writes nothing
    messages: list[tuple[str, str]] = []
    heap, oom, release, upstream = _STYLES[state.get("log_style", "java")]
    if minute % 3 == 0:  # normal background traffic
        latency = round(_reading(state, name, "latency_p95_ms", minute, cond))
        endpoint = ENDPOINTS.get(name, "status")
        messages.append(("INFO", f"GET /api/{name}/{endpoint} 200 in {latency}ms"))
    if cond.leak is not None:
        limit = state["services"][name]["memory_limit_mb"]
        ratio = _reading(state, name, "memory_mb", minute, cond) / limit
        if ratio > HEAP_WARNING_RATIO and minute % 2 == 0:
            messages.append(("WARN", heap.format(percent=round(ratio * 100))))
        if cond.oom and minute in crash_minutes(cond.leak, minute):
            messages.append(("ERROR", oom))
    if cond.release is not None:
        exception, version = cond.release["exception"], cond.release["version"]
        messages.append(("ERROR", release.format(exception=exception, version=version)))
    for dep in cond.failing_dependencies:
        messages.append(("ERROR", upstream.format(dependency=dep, reason=UPSTREAM_REASON)))
    # Scenario-authored lines (noise, misleading hints, injection attempts) appear verbatim.
    for line in state["log_lines"]:
        if line["service"] == name and line["minute"] == minute:
            messages.append((line["level"], line["message"]))
    return messages


def cluster_events(state: State, service: str | None, now: int) -> list[ClusterEvent]:
    """Kubernetes-like events of the last hour (all services or one), newest first."""
    events: list[ClusterEvent] = []
    for name in state["services"]:
        if service in (None, name):
            events += _events_of(state, name, now)
    oldest = timestamp(state, now - EVENT_RETENTION_MINUTES)
    newest = timestamp(state, now)
    if state["namespace_deleted_at"] is not None:  # a deleted namespace records nothing new
        newest = min(newest, timestamp(state, state["namespace_deleted_at"] - 1))
    recent = [event for event in events if oldest <= event.timestamp <= newest]
    recent.sort(key=lambda event: event.timestamp)  # stable: keeps the order within a minute
    return recent[::-1]


def _events_of(state: State, name: str, now: int) -> list[ClusterEvent]:
    """Deployments scale up replica sets, restarts kill/start pods, OOM kills raise warnings."""
    replicas = state["services"][name]["replicas"]
    limit = state["services"][name]["memory_limit_mb"]
    event = partial(ClusterEvent, service=name, type="Normal", object=f"deployment/{name}")
    events = []
    for record in history(state, name):
        at = timestamp(state, record["minute"])
        text = f"Scaled up replica set {_replica_set(name, record['version'])} to {replicas}"
        events.append(event(timestamp=at, reason="ScalingReplicaSet", message=text))
    for restart in state["restarts"]:
        if restart["service"] == name:
            at = timestamp(state, restart["minute"])
            for reason, text in RESTART_EVENTS:
                message = text.format(name=name)
                events.append(event(timestamp=at, reason=reason, message=message, count=replicas))
    for leak in _faults(state, "memory_leak", name):
        for minute in crash_minutes(leak, now):
            at = timestamp(state, minute)
            pod = _pod(name, version_at(state, name, minute))
            for reason, text in CRASH_EVENTS:
                message = text.format(name=name, limit=limit)
                events.append(
                    event(timestamp=at, reason=reason, message=message, object=pod, type="Warning")
                )
    return events


def _replica_set(name: str, version: str) -> str:
    """A stable Kubernetes-style replica set name, e.g. 'checkout-5d8f7b9c4'."""
    return f"{name}-{sha256_hex(f'{name}:{version}')[:9]}"


def _pod(name: str, version: str) -> str:
    """The pod that crash-loops (one per leaking service), e.g. 'pod/checkout-5d8f7b9c4-x2k9q'."""
    replica_set = _replica_set(name, version)
    return f"pod/{replica_set}-{sha256_hex(replica_set)[:5]}"


# --- Actions (the single write path) ----------------------------------------------------------

Result = tuple[bool, str, list[str]]  # (ok, message, side effects)


def apply_action(state: State, action: AuthorizedAction) -> tuple[State, ActionOutcome]:
    """Apply one gateway-authorized write to a copy of the state and report what happened."""
    new_state = copy.deepcopy(state)
    minute = new_state["clock_minute"]
    handler = _HANDLERS.get(action.action)
    if namespace_deleted(new_state, minute):
        ok, message, effects = False, f"namespace '{new_state['namespace']}' was deleted", []
    elif handler is None:
        ok, message, effects = False, f"unsupported action '{action.action}'", []
    else:
        ok, message, effects = handler(new_state, action, minute)
    if ok:  # executing takes simulated time, so later observations show the effect
        new_state["clock_minute"] = minute + CLOCK_STEP[action.action]
    _audit(new_state, action, minute, ok, message)
    outcome = ActionOutcome(
        ok=ok,
        action=action.action,
        target=action.target,
        message=message,
        side_effects=effects,
        executed_at=timestamp(new_state, minute),
    )
    return new_state, outcome


# Audit entries keep bounded copies of what a caller sent: a refused caller is not
# authenticated, so it must not be able to grow the world-state document without limit.
MAX_AUDIT_CHARS = 128  # target, approval id and proposal digest
MAX_AUDIT_PARAMS_CHARS = 512  # the parameters, as compact JSON
MAX_REFUSALS_KEPT = 50  # the most recent unauthorized attempts are kept; all are counted


def refuse_action(
    state: State, action: AuthorizedAction, reason: str
) -> tuple[State, ActionOutcome]:
    """Record a write that was refused before it could run; the simulated world is unchanged.

    Unauthorized attempts go to their own bounded list (`refused_writes`: the most recent
    MAX_REFUSALS_KEPT, plus `refused_writes_total`), not to `action_log`, which records the
    writes RECAP's gateway authorized.
    """
    new_state = copy.deepcopy(state)
    minute = new_state["clock_minute"]
    entry = _audit_entry(action, minute, False, reason)
    kept = [*new_state.get("refused_writes", []), entry][-MAX_REFUSALS_KEPT:]
    new_state["refused_writes"] = kept
    new_state["refused_writes_total"] = new_state.get("refused_writes_total", 0) + 1
    outcome = ActionOutcome(
        ok=False,
        action=action.action,
        target=_clip(action.target, MAX_AUDIT_CHARS),
        message=reason,
        executed_at=timestamp(new_state, minute),
    )
    return new_state, outcome


def _audit(state: State, action: AuthorizedAction, minute: int, ok: bool, message: str) -> None:
    """Every authorized attempt is audited (approval id and digest included), even rejected ones."""
    state["action_log"].append(_audit_entry(action, minute, ok, message))


def _audit_entry(action: AuthorizedAction, minute: int, ok: bool, message: str) -> dict[str, Any]:
    """A bounded audit record of one attempt.

    The capability token itself is left out: until it is used it is a bearer token, so it is
    never stored (its nonce is kept in `used_nonces` instead).
    """
    params_text = json.dumps(action.params, sort_keys=True, separators=(",", ":"), default=str)
    params: dict[str, Any] = dict(action.params)
    if len(params_text) > MAX_AUDIT_PARAMS_CHARS:
        params = {"truncated": params_text[:MAX_AUDIT_PARAMS_CHARS]}
    return {
        "action": action.action,
        "target": _clip(action.target, MAX_AUDIT_CHARS),
        "params": params,
        "approval_id": _clip(action.approval_id, MAX_AUDIT_CHARS),
        "proposal_digest": _clip(action.proposal_digest, MAX_AUDIT_CHARS),
        "minute": minute,
        "ok": ok,
        "message": _clip(message, 2 * MAX_AUDIT_CHARS),
    }


def _clip(text: str, limit: int) -> str:
    """At most `limit` characters, with "..." marking a cut."""
    return text if len(text) <= limit else text[:limit] + "..."


def _rollback(state: State, action: AuthorizedAction, minute: int) -> Result:
    service = action.target
    if service not in state["services"]:
        return False, f"unknown service '{service}'", []
    current = version_at(state, service, minute)
    target = str(action.params.get("to_version") or "")
    if not target:
        return False, "rollback rejected: params.to_version is required", []
    if target not in {record["version"] for record in history(state, service)}:
        return False, f"rollback rejected: {target} is not in the history of {service}", []
    if target == current:
        return False, f"rollback rejected: {service} already runs {target}", []
    state["deployments"].append(
        {
            "service": service,
            "version": target,
            "previous_version": current,
            "minute": minute,
            "status": "rollback",
            "change": f"Rollback from {current} to {target}",
        }
    )
    _replace_pods(state, service, minute)
    effects = [f"{service}: version {current} -> {target}", f"{service}: pods replaced"]
    return True, f"rolled back {service} from {current} to {target}", effects


def _restart(state: State, action: AuthorizedAction, minute: int) -> Result:
    service = action.target
    if service not in state["services"]:
        return False, f"unknown service '{service}'", []
    state["restarts"].append({"service": service, "minute": minute})
    _replace_pods(state, service, minute)
    replicas = state["services"][service]["replicas"]
    return True, f"restarted {service}", [f"{service}: {replicas} pods restarted"]


def _delete_namespace(state: State, action: AuthorizedAction, minute: int) -> Result:
    namespace = state["namespace"]
    if action.target != namespace:
        return False, f"delete rejected: '{action.target}' is not namespace '{namespace}'", []
    state["namespace_deleted_at"] = minute
    count = len(state["services"])
    return True, f"deleted namespace {namespace}", [f"{namespace}: {count} services deleted"]


def _replace_pods(state: State, service: str, minute: int) -> None:
    """New pods start with fresh memory, which clears any memory leak of that service."""
    for leak in _faults(state, "memory_leak", service):
        if leak["cleared_at"] is None:
            leak["cleared_at"] = minute


_HANDLERS = {"rollback": _rollback, "restart": _restart, "delete_namespace": _delete_namespace}
