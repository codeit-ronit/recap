"""Optional Prometheus metrics for the API (spec section 10): off unless RECAP_METRICS=1.

With RECAP_METRICS=1 (or true, yes, on), GET /metrics answers in the Prometheus text
exposition format, version 0.0.4, written here by hand: no client library, no new
dependency. Without it, GET /metrics answers 404 and nothing is counted or timed.

    recap_api_requests_total             HTTP requests, by method, route template and
                                         status class (2xx, 4xx, ...)
    recap_api_request_duration_seconds   their time until the answer starts
    recap_active_incidents               incident runs created and not finished yet
    recap_incidents_finished_total       incident runs finished, by final state
    recap_incident_duration_seconds      their time from creation to that state
    recap_tool_calls_total               tool calls through the safety gateway, by tool
                                         and outcome (ok, error, denied)
    recap_tool_call_duration_seconds     the time of each call that was sent
    recap_model_requests_total           model requests as runs trace them, by provider class
    recap_model_request_duration_seconds their time as RECAP measured it, by provider class
    recap_model_tokens_total             tokens as the model server reported them, by
                                         provider class and direction (in, out)
    recap_actions_total                  fixes the controller decided on, by outcome
    recap_approval_wait_seconds          from an approval request to its decision, by decision
    recap_recovery_verifications_total   the recovery oracle's verdicts, by verified
    recap_environment_info               the target every run acts on: kind, simulated
    recap_queue_up                       with RECAP_EXECUTION_BACKEND=redis (M6): 1 when the
                                         Redis job queue answered this scrape, else 0
    recap_queue_depth, _capacity         jobs waiting, and how many may wait
    recap_queue_wait_seconds             how long new jobs waited for a worker (every worker's
                                         claims, one histogram kept in Redis)
    recap_workers                        registered workers, by whether their heartbeat is current
    recap_workers_busy                   live workers that hold a job now (worker occupancy)
    recap_jobs_total                     what happened to jobs, as the queue counts them across
                                         every API and worker process (submitted, completed, ...)

Where the numbers come from: an ASGI middleware (RequestMetrics) counts and times every
request, and ApiMetrics is one of the runtime's trace sinks while the app runs
(Runtime.extra_sinks): it counts what the events every run already writes to its audit
trail say (recap/telemetry/tracer.py). Nothing here changes a run or reads a record.

Labels are bounded: every label value comes from a closed set. A route is its template,
never the path (GET /runs/run-3f9a2c1b0d counts as /runs/{run_id}); a tool is one that
RECAP's policy catalog names (configs/policies.yaml); a provider class, a final state or an
outcome is one of RECAP's own. Anything else (a tool name a model or an MCP server made up,
an unusual HTTP method, a path no route serves) counts as "other". No label ever holds a
run, incident or proposal id, a prompt, model text, a credential, a path or other free text.

What the counts cover: this server process, from its start. Runs started by the CLI, the
evaluation runner or another process are not in them, nor is a request for a host name the
API does not serve (refused with 400 before anything else). With the Redis backend, runs
execute in workers, so the run-event counts above stay at 0 here; the recap_queue_* /
recap_workers* / recap_jobs_total families are read from Redis at each scrape and cover every
process. Each worker serves its own runs' families (the same names, without the HTTP ones) on
its own port when RECAP_METRICS=1 and RECAP_WORKER_METRICS_PORT are set (apps/worker/metrics.py);
Prometheus adds them up across workers. The scripted baseline is not a
model and sends no model requests; a replay's model requests and tokens are those of the
recorded run, answered offline from its recording (provider_class="replay"): no model is
called.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

from starlette.routing import Match, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from recap.core.models import (
    TERMINAL_STATES,
    AgentRole,
    IncidentState,
    ProviderOrigin,
    TraceEvent,
    Verdict,
)
from recap.runtime import ENVIRONMENT_ADAPTERS, Runtime
from recap.telemetry.tracer import TraceCategory
from recap.workers.queue import WAIT_BUCKETS, JobQueue
from recap.workers.resp import RedisError

log = logging.getLogger(__name__)

ENV_FLAG = "RECAP_METRICS"
PROMETHEUS_TEXT = "text/plain; version=0.0.4; charset=utf-8"  # the text exposition format
OTHER = "other"  # what a value outside a label's closed set is counted as

HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
FINAL_STATES = tuple(state.value for state in IncidentState if state in TERMINAL_STATES)
PROVIDER_CLASSES: tuple[str, ...] = get_args(ProviderOrigin)
TOOL_OUTCOMES = ("ok", "error", "denied")
ACTION_OUTCOMES = ("executed", "refused", "blocked", "rejected", "expired")
# How an approval request ended (recap/harness/controller.py: approval.decided).
APPROVAL_DECISIONS = ("approved", "rejected", "expired", "refused")
# What the job queue counts (recap/workers/queue.py: its stats hash), a closed set.
JOB_EVENTS = (
    "submitted",
    "completed",
    "failed",
    "cancelled",
    "needs_reconciliation",
    "requeued",
)
# Fixed buckets, in seconds: requests and tool calls, then whole incidents (a run on a real
# cluster, with a person approving its fix, takes minutes).
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
INCIDENT_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)
# A model request: under a second from a hosted model, minutes from a CPU-only local one (M6 W5
# measured 103 to 616 s on the owner's VM), at most the request timeout (900 s by default).
MODEL_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0)
# A person's approval: seconds at a terminal, up to the request's expiry (900 s by default).
APPROVAL_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0)


# ======================================================================================
# The text exposition format
# ======================================================================================

LabelValues = tuple[str, ...]  # one series' label values, in its family's label order
Sample = tuple[str, list[tuple[str, str]], float]  # name, (label, value) pairs, value


class Family:
    """One metric family: its name, help text, type and label names, and its samples."""

    kind = "untyped"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.labels = tuple(labels)

    def key(self, labels: dict[str, str]) -> LabelValues:
        """The label values in order. A missing or unexpected label is a bug: KeyError."""
        if labels.keys() != set(self.labels):
            raise KeyError(f"{self.name} takes the labels {self.labels}, not {sorted(labels)}")
        return tuple(labels[name] for name in self.labels)

    def pairs(self, key: LabelValues) -> list[tuple[str, str]]:
        return list(zip(self.labels, key, strict=True))

    def samples(self) -> Iterator[Sample]:
        raise NotImplementedError


class _Values(Family):
    """A family whose series each hold one number."""

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self.values: dict[LabelValues, float] = {}

    def samples(self) -> Iterator[Sample]:
        for key, value in sorted(self.values.items()):
            yield self.name, self.pairs(key), value


class Counter(_Values):
    """A count that only goes up."""

    kind = "counter"

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self.key(labels)
        self.values[key] = self.values.get(key, 0.0) + amount

    def set_total(self, value: float, **labels: str) -> None:
        """A total counted elsewhere (the Redis queue's counters), copied as it is."""
        self.values[self.key(labels)] = value

    def touch(self, **labels: str) -> None:
        """Show a series at 0 before anything happened (one value of a small closed set)."""
        self.values.setdefault(self.key(labels), 0.0)


class Gauge(_Values):
    """A value that is set, not added to."""

    kind = "gauge"

    def set(self, value: float, **labels: str) -> None:
        self.values[self.key(labels)] = value


@dataclass
class _Observed:
    """One histogram series: cumulative bucket counts, the sum and the count."""

    buckets: list[int]
    total: float = 0.0
    count: int = 0


class Histogram(Family):
    """Observations counted into fixed, cumulative buckets (le), with their sum and count."""

    kind = "histogram"

    def __init__(
        self, name: str, help_text: str, labels: Sequence[str], bounds: Sequence[float]
    ) -> None:
        super().__init__(name, help_text, labels)
        self.bounds = tuple(sorted(bounds))
        self.series: dict[LabelValues, _Observed] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self.key(labels)
        series = self.series.get(key)
        if series is None:
            series = self.series[key] = _Observed([0] * len(self.bounds))
        for index, bound in enumerate(self.bounds):
            if value <= bound:
                series.buckets[index] += 1
        series.total += value
        series.count += 1

    def set_series(self, buckets: Sequence[int], total: float, count: int, **labels: str) -> None:
        """A histogram observed elsewhere (the queue's, kept in Redis), copied as it is:
        cumulative counts per bound, in the bounds' order, with the sum and the count."""
        if len(buckets) != len(self.bounds):
            raise ValueError(f"{self.name} has {len(self.bounds)} buckets, not {len(buckets)}")
        self.series[self.key(labels)] = _Observed(list(buckets), total, count)

    def samples(self) -> Iterator[Sample]:
        for key, series in sorted(self.series.items(), key=lambda item: item[0]):
            pairs = self.pairs(key)
            for bound, count in zip(self.bounds, series.buckets, strict=True):
                yield f"{self.name}_bucket", [*pairs, ("le", _number(bound))], count
            yield f"{self.name}_bucket", [*pairs, ("le", "+Inf")], series.count
            yield f"{self.name}_sum", pairs, series.total
            yield f"{self.name}_count", pairs, series.count


def render_text(families: Iterable[Family]) -> str:
    """`families` in the Prometheus text exposition format (0.0.4): HELP, TYPE, samples."""
    lines: list[str] = []
    for family in families:
        lines.append(f"# HELP {family.name} {escape_help(family.help)}")
        lines.append(f"# TYPE {family.name} {family.kind}")
        for name, pairs, value in family.samples():
            labels = ",".join(f'{label}="{escape_label(text)}"' for label, text in pairs)
            series = f"{name}{{{labels}}}" if labels else name
            lines.append(f"{series} {_number(value)}")
    return "\n".join(lines) + "\n"


def escape_label(value: str) -> str:
    """A label value as the format quotes it: backslash, double quote and line feed escaped."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def escape_help(text: str) -> str:
    """HELP text: backslash and line feed escaped (a double quote stays as it is)."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _number(value: float) -> str:
    """A sample value or bucket bound: 3, 0.25, +Inf (whole numbers without a decimal point)."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 2**53:
        return str(int(value))
    return repr(float(value))


# ======================================================================================
# Bounded label values
# ======================================================================================


def bounded(value: object, allowed: Collection[str]) -> str:
    """`value` if it is one of `allowed`, else "other": a label never takes an outside value."""
    return value if isinstance(value, str) and value in allowed else OTHER


def route_template(scope: Scope) -> str:
    """The template of the route that answered a request, e.g. /runs/{run_id}: never its path.

    A request a middleware answered before routing (a refused cross-origin write, a CORS
    preflight) is matched against the app's routes here, as the router would have matched
    it. Static files and a path no route serves count as "other".
    """
    route = scope.get("route")
    if route is None:
        partial = None
        for candidate in getattr(scope.get("app"), "routes", ()):
            match, _ = candidate.matches(scope)
            if match == Match.FULL:
                route = candidate
                break
            if match == Match.PARTIAL and partial is None:
                partial = candidate
        route = route or partial
    return route.path if isinstance(route, Route) else OTHER


def _seconds(duration_ms: float | None) -> float | None:
    """Milliseconds as seconds; None for a missing or impossible duration."""
    if duration_ms is None or not math.isfinite(duration_ms) or duration_ms < 0:
        return None
    return duration_ms / 1000


# ======================================================================================
# RECAP's metrics
# ======================================================================================


class ApiMetrics:
    """RECAP's operational metrics for one API app (enabled only by RECAP_METRICS=1).

    RequestMetrics (below) hands it every HTTP request; as a trace sink it counts what every
    run's events say. One lock guards each update and the exposition: runs write from the
    event loop, while GET /metrics reads from a worker thread.
    """

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._tools: frozenset[str] = frozenset()  # RECAP's policy catalog (see watching)
        self._created: dict[str, datetime] = {}  # run id -> creation, until the run finishes
        self._asked: dict[str, datetime] = {}  # run id -> its open approval request's time
        self.requests = Counter(
            "recap_api_requests_total",
            "HTTP requests answered, by method, route template (never the path) and status "
            "class; static files and paths no route serves count as other.",
            ("method", "route", "status_class"),
        )
        self.request_seconds = Histogram(
            "recap_api_request_duration_seconds",
            "Time from receiving an HTTP request until its answer starts (a server-sent-events "
            "stream is timed to its start), by method and route template.",
            ("method", "route"),
            LATENCY_BUCKETS,
        )
        self.active = Gauge(
            "recap_active_incidents",
            "Incident runs this server created that have not reached a final state yet.",
        )
        self.finished = Counter(
            "recap_incidents_finished_total",
            "Incident runs that reached a final state, by that state.",
            ("state",),
        )
        self.incident_seconds = Histogram(
            "recap_incident_duration_seconds",
            "Wall time of an incident run from its creation to its final state, time spent "
            "waiting for a human approval included, by that state.",
            ("state",),
            INCIDENT_BUCKETS,
        )
        self.tool_calls = Counter(
            "recap_tool_calls_total",
            "Tool calls judged by RECAP's safety gateway, by tool (RECAP's policy catalog; any "
            "other name counts as other) and outcome: ok, error (sent and failed), denied "
            "(never sent). search_runbooks is answered by RECAP itself, every other tool by "
            "the MCP server.",
            ("tool", "outcome"),
        )
        self.tool_seconds = Histogram(
            "recap_tool_call_duration_seconds",
            "Time of each tool call the gateway sent, by tool.",
            ("tool",),
            LATENCY_BUCKETS,
        )
        self.model_requests = Counter(
            "recap_model_requests_total",
            "Model requests as runs trace them (llm.call events), by provider class. replay: "
            "answered offline from a committed recording, no model was called. The scripted "
            "baseline is not a model and sends none.",
            ("provider_class",),
        )
        self.model_seconds = Histogram(
            "recap_model_request_duration_seconds",
            "Time of each model request as RECAP measured it, from sending to the whole answer, "
            "by provider class (replay: the recorded request's time, nothing was waited for).",
            ("provider_class",),
            MODEL_BUCKETS,
        )
        self.model_tokens = Counter(
            "recap_model_tokens_total",
            "Model tokens as the model server reported them (never estimated), by provider "
            "class and direction (in, out); for replay, the recorded run's counts.",
            ("provider_class", "direction"),
        )
        self.actions = Counter(
            "recap_actions_total",
            "Fixes the controller decided on, by outcome: executed (sent, and the target "
            "applied it), refused (sent, but the target refused it or did not confirm it), "
            "blocked (RECAP's safety policy or evidence gate stopped it before anything was "
            "sent), rejected or expired (the person's approval).",
            ("outcome",),
        )
        self.approval_seconds = Histogram(
            "recap_approval_wait_seconds",
            "Time from a fix's approval request to its decision, by decision: approved, "
            "rejected, expired (nobody answered in time), refused (the request could not be "
            "made). The run's own budget clock is paused while it waits.",
            ("decision",),
            APPROVAL_BUCKETS,
        )
        self.recoveries = Counter(
            "recap_recovery_verifications_total",
            "The recovery oracle's verdict after each fix that was sent, read from the "
            "environment's own health (never from agent text), by whether recovery was "
            "verified.",
            ("verified",),
        )
        self.environment = Gauge(
            "recap_environment_info",
            "The environment every run of this server acts on (always 1): its adapter kind "
            "and whether it is simulated.",
            ("kind", "simulated"),
        )
        self.queue_up = Gauge(
            "recap_queue_up",
            "With the Redis execution backend: 1 when the job queue answered this scrape, 0 "
            "when it did not (the other queue metrics are then left out).",
        )
        self.queue_depth = Gauge("recap_queue_depth", "Jobs waiting in the Redis job queue.")
        self.queue_capacity = Gauge(
            "recap_queue_capacity",
            "How many jobs may wait; a submission beyond it is refused with 429.",
        )
        self.workers = Gauge(
            "recap_workers",
            "Registered workers, by whether their heartbeat is current (a dead worker's jobs "
            "are settled by another worker from the runs' persisted evidence).",
            ("alive",),
        )
        self.workers_busy = Gauge(
            "recap_workers_busy",
            "Live workers that hold at least one job now (worker occupancy; compare with "
            'recap_workers{alive="true"}).',
        )
        self.queue_wait = Histogram(
            "recap_queue_wait_seconds",
            "How long new jobs waited in the Redis queue for their first claim by a worker, as "
            "every worker recorded it in Redis (a retried job's later waits are not counted).",
            (),
            WAIT_BUCKETS,
        )
        self.jobs = Counter(
            "recap_jobs_total",
            "What happened to jobs, as the Redis queue counts them across every API and worker "
            "process: submitted, completed (the harness ran the incident to its end; the run "
            "record says how it went), failed, cancelled, needs_reconciliation (a fix may have "
            "reached the target; a person checks), requeued (interrupted before any write).",
            ("event",),
        )
        # The few outcomes that matter most are shown at 0 from the start.
        for state in FINAL_STATES:
            self.finished.touch(state=state)
        for outcome in ACTION_OUTCOMES:
            self.actions.touch(outcome=outcome)
        for verified in ("true", "false"):
            self.recoveries.touch(verified=verified)
        self._handlers: dict[str, Callable[[TraceEvent], None]] = {
            TraceCategory.STATE: self._state,
            TraceCategory.TOOL_RESULT: self._tool_call,
            TraceCategory.MITIGATION: self._write,
            TraceCategory.POLICY: self._policy,
            TraceCategory.VERIFY_EVIDENCE: self._evidence_gate,
            TraceCategory.APPROVAL_REQUESTED: self._approval_asked,
            TraceCategory.APPROVAL_DECIDED: self._approval,
            TraceCategory.VERIFY_RECOVERY: self._recovery,
            TraceCategory.LLM_CALL: self._model_call,
        }

    @classmethod
    def from_environment(cls) -> ApiMetrics:
        """Enabled by RECAP_METRICS=1 (also true, yes or on), like RECAP's other switches."""
        flag = os.environ.get(ENV_FLAG, "").strip().lower()
        return cls(enabled=flag in {"1", "true", "yes", "on"})

    @contextmanager
    def watching(self, runtime: Runtime) -> Iterator[None]:
        """While the app runs, count the events of every run `runtime` starts: this object is
        one of its trace sinks (Runtime.extra_sinks) until the app stops. Nothing when off."""
        if not self.enabled:
            yield
            return
        with self._lock:
            self._tools = frozenset(runtime.settings.policies.tools)
            self._describe(runtime)
        runtime.extra_sinks.append(self)
        try:
            yield
        finally:
            if self in runtime.extra_sinks:
                runtime.extra_sinks.remove(self)

    def observe_request(self, scope: Scope, status: int, seconds: float) -> None:
        """Count one answered HTTP request and its time until the answer started."""
        method = bounded(scope.get("method"), HTTP_METHODS)
        route = route_template(scope)
        family = status // 100
        status_class = f"{family}xx" if 2 <= family <= 5 else OTHER
        with self._lock:
            self.requests.inc(method=method, route=route, status_class=status_class)
            self.request_seconds.observe(seconds, method=method, route=route)

    def write(self, event: TraceEvent) -> None:
        """Trace sink: count what one run event says (an odd payload counts as "other")."""
        handler = self._handlers.get(event.category)
        if handler is not None:
            with self._lock:
                handler(event)

    def render(self, queue: JobQueue | None = None, *, http: bool = True) -> str:
        """Every metric, in the Prometheus text format; with `queue` (the Redis backend), the
        queue's, read from Redis now. A worker renders without the HTTP families (http=False):
        it serves no API."""
        snapshot = waits = None
        if queue is not None:
            try:
                snapshot = queue.snapshot(recent=0)
                waits = queue.queue_waits()
            except RedisError:
                snapshot = waits = None
        with self._lock:
            self.active.set(len(self._created))
            families: list[Family] = [self.requests, self.request_seconds] if http else []
            families += [
                self.active,
                self.finished,
                self.incident_seconds,
                self.tool_calls,
                self.tool_seconds,
                self.model_requests,
                self.model_seconds,
                self.model_tokens,
                self.actions,
                self.approval_seconds,
                self.recoveries,
                self.environment,
            ]
            if queue is not None:
                families += self._queue(snapshot, waits)
            return render_text(families)

    def _queue(
        self, snapshot: dict[str, Any] | None, waits: tuple[list[int], int, float] | None
    ) -> list[Family]:
        self.queue_up.set(1 if snapshot is not None and waits is not None else 0)
        if snapshot is None or waits is None:
            return [self.queue_up]
        self.queue_depth.set(snapshot["depth"])
        self.queue_capacity.set(snapshot["capacity"])
        workers = snapshot["workers"]
        alive = sum(1 for worker in workers if worker["alive"])
        self.workers.set(alive, alive="true")
        self.workers.set(len(workers) - alive, alive="false")
        self.workers_busy.set(sum(1 for worker in workers if worker["alive"] and worker["jobs"]))
        buckets, count, total = waits
        self.queue_wait.set_series(buckets, total, count)
        counters = snapshot["counters"]
        for event in JOB_EVENTS:  # a total read from Redis: set, not added to
            self.jobs.set_total(float(counters.get(event, 0)), event=event)
        return [
            self.queue_up,
            self.queue_depth,
            self.queue_capacity,
            self.queue_wait,
            self.workers,
            self.workers_busy,
            self.jobs,
        ]

    # --- what run events say ----------------------------------------------------------------

    def _describe(self, runtime: Runtime) -> None:
        """Which target every run of `runtime` acts on. A live target is never "simulated"."""
        try:
            info = runtime.environment_info()  # describes the adapter, contacts no cluster
        except Exception:  # GET /metrics still answers; the target is then left out
            log.exception("the environment could not describe itself for the metrics")
            return
        kind = bounded(info.kind, ENVIRONMENT_ADAPTERS)
        self.environment.set(1, kind=kind, simulated="true" if info.is_simulated else "false")

    def _state(self, event: TraceEvent) -> None:
        """An incident is active from its creation until it reaches a final state."""
        payload = event.payload
        to = payload.get("to")
        if to == IncidentState.CREATED.value and payload.get("from") is None:
            self._created[event.run_id] = event.ts
        elif isinstance(to, str) and to in FINAL_STATES:
            self.finished.inc(state=to)
            self._asked.pop(event.run_id, None)  # a request the run left behind
            created = self._created.pop(event.run_id, None)
            if created is not None:
                seconds = max(0.0, (event.ts - created).total_seconds())
                self.incident_seconds.observe(seconds, state=to)

    def _tool_call(self, event: TraceEvent) -> None:
        """A call the gateway sent: a read's tool.result, or a write's mitigation.executed."""
        tool = bounded(event.payload.get("tool"), self._tools)
        outcome = "ok" if event.payload.get("ok") is True else "error"
        self.tool_calls.inc(tool=tool, outcome=outcome)
        seconds = _seconds(event.duration_ms)
        if seconds is not None:
            self.tool_seconds.observe(seconds, tool=tool)

    def _write(self, event: TraceEvent) -> None:
        """A fix the gateway sent: executed when the target applied it, else refused."""
        self._tool_call(event)
        self.actions.inc(outcome="executed" if event.payload.get("ok") is True else "refused")

    def _policy(self, event: TraceEvent) -> None:
        """A denial: the call was never sent. A call by the controller is a fix it decided on."""
        payload = event.payload
        if payload.get("verdict") != Verdict.DENY.value:
            return
        self.tool_calls.inc(tool=bounded(payload.get("tool"), self._tools), outcome="denied")
        if payload.get("caller") == AgentRole.CONTROLLER.value:
            self.actions.inc(outcome="blocked")

    def _evidence_gate(self, event: TraceEvent) -> None:
        """The evidence gate stopped a fix (it blocks only when enforced, as through the API)."""
        payload = event.payload
        check = payload.get("proposal_check")
        if isinstance(check, dict) and check.get("ok") is False and payload.get("enforced") is True:
            self.actions.inc(outcome="blocked")

    def _approval_asked(self, event: TraceEvent) -> None:
        """A run asks a person: one request at a time (the run waits for its decision)."""
        self._asked[event.run_id] = event.ts

    def _approval(self, event: TraceEvent) -> None:
        """A decision: its wait, and a fix the person rejected or whose request expired."""
        decision = event.payload.get("decision")
        asked = self._asked.pop(event.run_id, None)
        if asked is not None:
            seconds = max(0.0, (event.ts - asked).total_seconds())
            self.approval_seconds.observe(seconds, decision=bounded(decision, APPROVAL_DECISIONS))
        if decision in ("rejected", "expired"):
            self.actions.inc(outcome=decision)

    def _recovery(self, event: TraceEvent) -> None:
        verified = "true" if event.payload.get("verified") is True else "false"
        self.recoveries.inc(verified=verified)

    def _model_call(self, event: TraceEvent) -> None:
        """One request a model provider sent, or a replay answered from its recording."""
        payload = event.payload
        provider_class = bounded(payload.get("origin"), PROVIDER_CLASSES)
        self.model_requests.inc(provider_class=provider_class)
        seconds = _seconds(event.duration_ms)
        if seconds is not None:
            self.model_seconds.observe(seconds, provider_class=provider_class)
        for direction, name in (("in", "input_tokens"), ("out", "output_tokens")):
            tokens = payload.get(name)
            # Only what the server reported: None means it reported nothing (never estimated).
            if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
                self.model_tokens.inc(tokens, provider_class=provider_class, direction=direction)


# ======================================================================================
# The request middleware
# ======================================================================================


class RequestMetrics:
    """ASGI middleware: hands each HTTP request's method, route, status and time to ApiMetrics.

    The time runs until the answer starts (its status line and headers): a server-sent-events
    stream (GET /runs/{id}/stream) stays open for as long as its client watches, so the time
    to its end would say nothing about the server. A request the app fails on before it
    answers is counted as the 500 the server then sends.
    """

    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        answered = False

        def count(status: int) -> None:
            try:
                self.metrics.observe_request(scope, status, time.perf_counter() - started)
            except Exception:  # counting must never break an answer
                log.exception("request metrics failed")

        async def send_and_count(message: Message) -> None:
            nonlocal answered
            if message["type"] == "http.response.start" and not answered:
                answered = True
                count(int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, send_and_count)
        except Exception:
            if not answered:  # the server's error middleware answers 500
                count(500)
            raise
