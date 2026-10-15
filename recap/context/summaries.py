"""Evidence summaries: the short, structured text a provider reads for each tool result.

Where it sits:

    gateway  -> Evidence (.content = raw JSON text, .data = the structured observation)
    packer   -> [render_for_agents(evidence, incident_time)] -> ContextItem.text -> prompt

Why it exists (audit finding 2.1): tool results are long and chronological, oldest first.
V1 put the raw JSON into the context pack and cut it to its first ~1,600 characters, so
the newest lines, the ones that explain an incident, were the first to go. On the real
simulator not one of 18 ERROR lines survived. A summary puts the facts that matter into a
few hundred characters instead:

    logs        lines and levels counted, when the first ERROR appeared (and how long before
                the alert), the newest ERROR/WARN lines with repeats collapsed, and the most
                frequent message shapes
    metrics     first, min, max and latest value with their times, the largest jump between
                two points, and the latest/max value as a share of the limit (e.g. memory)
    health      status, ready replicas, error rate and p95 latency on one line
    deployments newest first, with the previous version and the time relative to the alert
    events      grouped by service, type and reason, with a count and the latest message
    services, rollback plans, action outcomes: one short line each

Rules this module keeps:

* Deterministic: the same evidence always gives exactly the same text.
* Environment-agnostic: it reads the observation models of recap/core/observations.py,
  never the simulator's message templates or thresholds, and it states what was observed,
  never a diagnosis. Deciding what the facts mean is the provider's and verifier's job.
* Bounded: a summary is fitted into `max_chars` by showing fewer list entries, so the
  newest lines are never the part that gets cut.
* Nothing is lost: each summary ends with the evidence ID. Evidence.content keeps the
  raw record for the registry, the dashboard and audits.

Anything this module does not know (an unknown tool, data that does not fit its model,
plain text) is passed through as the raw content; the packer then keeps its head and tail.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from recap.core.models import Evidence
from recap.core.observations import (
    ActionOutcome,
    ClusterEvent,
    DeploymentList,
    EventList,
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)
from recap.core.signals import as_utc, minutes_between

Model = TypeVar("Model", bound=BaseModel)

MAX_MESSAGE_CHARS = 160  # one log message or event message in a summary
# How many list entries (log lines, events, deployments, ...) to try, from most to fewest,
# until the summary fits its character budget.
ENTRY_LIMITS = (8, 6, 4, 3, 2, 1)
TOP_TEMPLATES = 3  # most frequent log message shapes
NOTABLE_LEVELS = ("ERROR", "WARN")
LEVEL_ORDER = ("ERROR", "WARN", "INFO", "DEBUG")

# Message "shapes": numbers become "#" and long hex IDs "#id", so "timeout after 3000ms" and
# "timeout after 2950ms" count as one. Digits right after a letter or a dot are kept, so a
# version such as "v2.4.1" stays readable. ("<n>" would be HTML-escaped inside prompts.)
_HEX_ID_RE = re.compile(r"\b(?:0x[0-9a-fA-F]+|[0-9a-f]{8,})\b")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?")


def render_for_agents(
    evidence: Evidence, incident_time: datetime | None = None, max_chars: int | None = None
) -> str:
    """The text a provider sees for one piece of evidence (a summary when the tool is known).

    `incident_time` (the alert time) lets the summary say how long before or after the
    alert something happened. `max_chars` is the room the packer has for one item.
    """
    summarize = _SUMMARIES.get(evidence.tool_name or "")
    if summarize is None:
        return evidence.content
    model, render = summarize
    observation = _parse(model, evidence.data)
    if observation is None:
        return evidence.content  # the data does not have the expected shape: show it raw
    footer = f"raw evidence: {evidence.id}"
    text = ""
    for limit in ENTRY_LIMITS:
        text = "\n".join([*render(observation, incident_time, limit), footer])
        if max_chars is None or len(text) <= max_chars:
            break
    return text


# --- logs -----------------------------------------------------------------------------------


def _logs(result: LogQueryResult, alert: datetime | None, limit: int) -> list[str]:
    lines = result.lines
    header = f"Logs of {result.service}: {len(lines)} lines returned"
    if result.total_matched > len(lines):
        header += f" ({result.total_matched} matched)"
    if not lines:
        return [header + "."]
    header += f", {_stamp(lines[0].timestamp, alert)} to {_stamp(lines[-1].timestamp, alert)}"
    levels = Counter(line.level for line in lines)
    counts = ", ".join(f"{levels[level]} {level}" for level in LEVEL_ORDER if levels[level])
    out = [header, f"levels: {counts}"]
    errors = [line for line in lines if line.level == "ERROR"]
    if errors:
        first = errors[0]
        out.append(f"first ERROR: {_when(first.timestamp, alert)}: {_message(first.message)}")
    notable = [line for line in lines if line.level in NOTABLE_LEVELS]
    if notable:
        out.append(f"newest {'/'.join(NOTABLE_LEVELS)} lines (newest last):")
        out += [f"  {entry}" for entry in _newest_groups(notable, alert, limit)]
    out += _frequent_shapes(lines)
    return out


def _newest_groups(lines: Sequence[LogLine], alert: datetime | None, limit: int) -> list[str]:
    """The `limit` newest distinct (level, message) pairs; repeats are collapsed with ×k."""
    groups: dict[tuple[str, str], list[LogLine]] = {}
    for line in lines:  # chronological, so each group's list ends with its newest line
        groups.setdefault((line.level, line.message), []).append(line)
    newest = sorted(groups.values(), key=lambda group: group[-1].timestamp)[-limit:]
    entries = []
    for group in newest:
        last = group[-1]
        when = _stamp(last.timestamp, alert)
        if len(group) > 1:
            when = f"{_stamp(group[0].timestamp, alert)}-{when} ×{len(group)}"
        entries.append(f"{when} {last.level} {_message(last.message)}")
    return entries


def _frequent_shapes(lines: Sequence[LogLine]) -> list[str]:
    """The most frequent message shapes, when something repeats (counts are a strong signal)."""
    shapes = Counter((line.level, _shape(line.message)) for line in lines)
    top = [(pair, count) for pair, count in shapes.most_common(TOP_TEMPLATES) if count > 1]
    if not top:
        return []
    return ["most frequent messages:"] + [
        f"  {count}× {level} {_message(shape)}" for (level, shape), count in top
    ]


def _shape(message: str) -> str:
    return _NUMBER_RE.sub("#", _HEX_ID_RE.sub("#id", message))


# --- metrics --------------------------------------------------------------------------------


def _metric(series: MetricSeries, alert: datetime | None, limit: int) -> list[str]:
    del limit  # a metric summary has a fixed size
    points = series.points
    header = f"Metric {series.metric} of {series.service} ({series.unit})"
    if not points:
        return [header + ": no data points."]
    first, last = points[0], points[-1]
    low = min(points, key=lambda point: point.value)
    high = max(points, key=lambda point: point.value)
    out = [
        f"{header}: {len(points)} points every {series.step_minutes} min, "
        f"{_stamp(first.timestamp, alert)} to {_stamp(last.timestamp, alert)}",
        f"first {_num(first.value)}; latest {_num(last.value)} at "
        f"{_stamp(last.timestamp, alert)} (change {_signed(last.value - first.value)})",
        f"min {_num(low.value)} at {_stamp(low.timestamp, alert)}; "
        f"max {_num(high.value)} at {_stamp(high.timestamp, alert)}",
    ]
    if low.value == high.value:
        out.append("all points are equal")
    else:
        before, after = max(
            zip(points, points[1:], strict=False),
            key=lambda pair: abs(pair[1].value - pair[0].value),
        )
        out.append(
            f"largest jump: {_signed(after.value - before.value)} from "
            f"{_stamp(before.timestamp, alert)} to {_when(after.timestamp, alert)}"
        )
    if series.limit:
        out.append(
            f"limit {_num(series.limit)} {series.unit}: latest at "
            f"{last.value / series.limit:.0%} of it, max at {high.value / series.limit:.0%}"
        )
    return out


# --- health, deployments, events, services, plans, outcomes ---------------------------------


def _health(snapshot: HealthSnapshot, alert: datetime | None, limit: int) -> list[str]:
    del limit
    return [
        f"Health of {snapshot.service} at {_stamp(snapshot.observed_at, alert)}: "
        f"{snapshot.status}, {snapshot.ready_replicas}/{snapshot.replicas} replicas ready, "
        f"error rate {_num(snapshot.error_rate)}, p95 latency {_num(snapshot.latency_p95_ms)} ms"
    ]


def _deployments(listing: DeploymentList, alert: datetime | None, limit: int) -> list[str]:
    records = listing.deployments  # newest first
    out = [f"Deployments (newest first): {min(limit, len(records))} shown of {len(records)}"]
    for record in records[:limit]:
        line = f"- {record.service} {record.version}"
        if record.previous_version:
            line += f" (previous {record.previous_version})"
        line += f", {record.status}, {_when(record.deployed_at, alert)}"
        if record.change_summary:
            line += f": {_message(record.change_summary)}"
        out.append(line)
    return out


def _events(listing: EventList, alert: datetime | None, limit: int) -> list[str]:
    groups: dict[tuple[str, str, str], list[ClusterEvent]] = {}
    for event in listing.events:
        groups.setdefault((event.service, event.type, event.reason), []).append(event)
    # Newest group first, like the event list itself.
    ordered = sorted(groups.values(), key=lambda group: max(e.timestamp for e in group))[::-1]
    total = sum(event.count for event in listing.events)
    out = [f"Cluster events: {_count(total, 'event')} in {_count(len(groups), 'group')}"]
    if groups:
        out[0] += " (newest first)"
    for group in ordered[:limit]:
        latest = max(group, key=lambda event: event.timestamp)
        count = sum(event.count for event in group)
        out.append(
            f"- {latest.service} {latest.type} {latest.reason} ×{count}, latest "
            f"{_when(latest.timestamp, alert)}: {_message(latest.message)}"
        )
    if len(ordered) > limit:
        out.append(f"- ({len(ordered) - limit} older groups not shown)")
    return out


def _services(listing: ServiceList, alert: datetime | None, limit: int) -> list[str]:
    del alert, limit  # a service list is short and has no times
    out = [f"Services in namespace {listing.namespace}: {len(listing.services)}"]
    for service in listing.services:
        calls = ", ".join(service.dependencies) or "nothing"
        out.append(
            f"- {service.name} {service.version}, {service.replicas} replicas, calls {calls}"
        )
    return out


def _rollback_plan(plan: RollbackPlan, alert: datetime | None, limit: int) -> list[str]:
    del alert, limit
    target = plan.target_version or "none"
    verdict = "possible" if plan.possible else "not possible"
    return [
        f"Rollback plan (dry run) for {plan.service}: {verdict}, "
        f"from {plan.current_version} to {target}; reason: {_message(plan.reason)}"
    ]


def _action_outcome(outcome: ActionOutcome, alert: datetime | None, limit: int) -> list[str]:
    del limit
    status = "done" if outcome.ok else "refused"
    out = [
        f"Action {outcome.action} on {outcome.target}: {status} at "
        f"{_stamp(outcome.executed_at, alert)}: {_message(outcome.message)}"
    ]
    if outcome.side_effects:
        out.append("side effects: " + "; ".join(_message(e) for e in outcome.side_effects))
    return out


Renderer = Callable[[Any, datetime | None, int], list[str]]

# Tool name (RECAP's trusted catalog) -> the observation model its data holds, and its renderer.
_SUMMARIES: dict[str, tuple[type[BaseModel], Renderer]] = {
    "get_service_logs": (LogQueryResult, _logs),
    "get_service_metrics": (MetricSeries, _metric),
    "get_service_health": (HealthSnapshot, _health),
    "get_recent_deployments": (DeploymentList, _deployments),
    "get_k8s_like_events": (EventList, _events),
    "list_services": (ServiceList, _services),
    "propose_rollback": (RollbackPlan, _rollback_plan),
    "execute_rollback": (ActionOutcome, _action_outcome),
    "restart_service": (ActionOutcome, _action_outcome),
    "delete_namespace": (ActionOutcome, _action_outcome),
}


# --- small helpers --------------------------------------------------------------------------


def _parse(model: type[Model], data: dict[str, Any]) -> Model | None:
    """Tool data is untrusted: unknown keys are ignored, and data that does not fit is None."""
    known = {key: value for key, value in data.items() if key in model.model_fields}
    try:
        return model.model_validate(known)
    except ValidationError:
        return None


def _message(text: str) -> str:
    """One message on one line, at most MAX_MESSAGE_CHARS long."""
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_MESSAGE_CHARS else flat[: MAX_MESSAGE_CHARS - 3] + "..."


def _stamp(moment: datetime, alert: datetime | None) -> str:
    """HH:MM on the alert's day (UTC), otherwise the date too."""
    moment = as_utc(moment)
    if alert is not None and moment.date() == as_utc(alert).date():
        return moment.strftime("%H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def _when(moment: datetime, alert: datetime | None) -> str:
    """A time plus its distance from the alert, e.g. '09:48 (12 min before the alert)'."""
    stamp = _stamp(moment, alert)
    if alert is None:
        return stamp
    minutes = minutes_between(moment, alert)  # positive: before the alert
    if abs(minutes) < 1:
        return f"{stamp} (at the alert)"
    side = "before" if minutes > 0 else "after"
    return f"{stamp} ({_duration(abs(minutes))} {side} the alert)"


def _duration(minutes: float) -> str:
    if minutes < 120:
        return f"{round(minutes)} min"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)} h"
    return f"{round(minutes / (24 * 60))} days"


def _num(value: float) -> str:
    """A number as short text: 3170, 2905.5, 0.3623, 0.002."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.4g}"


def _count(number: int, noun: str) -> str:
    """'1 event', '3 events'."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _signed(value: float) -> str:
    return ("+" if value >= 0 else "-") + _num(abs(value))
