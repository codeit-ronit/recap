"""Role playbooks of the scripted baseline policy (NOT an LLM): which read-only check is next.

Where it sits:

    DeterministicProvider.next_action --> [playbooks] --> the next ToolCall (one per step)

A playbook is an ordered checklist, like the first page of an on-call runbook. It is rebuilt
before every step from the evidence gathered so far, minus every call already made in this
run (by any agent), so an agent never repeats a call and new evidence can add follow-ups:

    investigator                 per target: health, logs, error_rate, deployments, events,
                                 memory_mb; then a rollback dry run for recently released targets
    telemetry_investigator       per target: health, logs, error_rate; then memory_mb for
                                 targets that crash, miss replicas or log heap/OOM trouble
    infrastructure_investigator  cluster-wide services, deployments and events; then rollback
                                 dry runs for recent releases and health of OOM-killed services

Targets are the alerted service, the other affected services and the verifier's focus
services, then the dependencies that "upstream X call failed" errors point to (followed
transitively). The investigator and telemetry roles look at three targets at most.

Memory-guided confirmation: if a very similar past incident was verified as fixed, the
investigator first runs the three checks that can confirm the same failure mechanism. When
live evidence agrees it may finish early (fewer tool calls); when it does not, the normal
playbook continues. A remembered answer never replaces live evidence. "Very similar" means a
memory similarity of at least `familiar_similarity`, which the provider passes in from
routing.familiar_similarity in configs/budget.yaml (the threshold the router uses too).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from recap.context.evidence import tool_source_ref
from recap.core.models import (
    AgentRole,
    EpisodeOutcome,
    FailureMechanism,
    InvestigationRequest,
    ToolCall,
)
from recap.core.signals import HEAP_WARNING_TEMPLATE, OOM_EVENT_REASONS, is_oom_message
from recap.providers.heuristics import (
    ParsedEvidence,
    generate_hypotheses,
    memory_target,
    parse_evidence,
)

MAX_TARGETS = 3  # the investigator and telemetry roles examine at most three services
_HEAP_WARNING = HEAP_WARNING_TEMPLATE.split("{")[0]  # "heap usage at "


class Check(NamedTuple):
    """One playbook step: a read-only tool, its fixed arguments and the question it answers."""

    tool: str
    args: dict[str, Any]
    question: str


HEALTH = Check("get_service_health", {}, "is {service} running with all replicas ready?")
LOGS = Check("get_service_logs", {}, "which errors does {service} log?")
ERROR_RATE = Check(
    "get_service_metrics", {"metric": "error_rate"}, "when did {service} start failing?"
)
DEPLOYMENTS = Check("get_recent_deployments", {}, "was {service} released recently?")
EVENTS = Check("get_k8s_like_events", {}, "any OOMKilled or BackOff events for {service}?")
MEMORY = Check(
    "get_service_metrics", {"metric": "memory_mb"}, "how close is {service} to its memory limit?"
)
ROLLBACK_PLAN = Check(
    "propose_rollback", {}, "which version would a rollback of {service} restore? (dry run)"
)
# Cluster-wide checks take no service argument.
ALL_SERVICES = Check("list_services", {}, "which services exist and what do they call?")
ALL_DEPLOYMENTS = Check(
    "get_recent_deployments", {"limit": 10}, "which services were released recently?"
)
ALL_EVENTS = Check(
    "get_k8s_like_events", {"limit": 30}, "any crashes or OOM kills in the namespace?"
)

INVESTIGATOR_CHECKS = (HEALTH, LOGS, ERROR_RATE, DEPLOYMENTS, EVENTS, MEMORY)
TELEMETRY_CHECKS = (HEALTH, LOGS, ERROR_RATE)
CLUSTER_CHECKS = (ALL_SERVICES, ALL_DEPLOYMENTS, ALL_EVENTS)
# The three checks that can confirm (or refute) a remembered failure mechanism.
CONFIRMATION_CHECKS: dict[FailureMechanism, tuple[Check, ...]] = {
    FailureMechanism.BAD_DEPLOYMENT: (DEPLOYMENTS, ERROR_RATE, LOGS),
    FailureMechanism.RESOURCE_PRESSURE: (EVENTS, MEMORY, LOGS),
}


class MemoryGuide(NamedTuple):
    """A verified past episode that steers the investigator's first checks."""

    episode_id: str
    mechanism: FailureMechanism
    target: str


def plan_next_calls(request: InvestigationRequest, familiar_similarity: float) -> list[ToolCall]:
    """The remaining ordered checks for this role, skipping every call already made in this run."""
    parsed = parse_evidence(request.evidence.values())
    playbook = PLAYBOOKS.get(request.role)
    planned = [
        *_memory_checks(request, familiar_similarity),
        *(playbook(request, parsed) if playbook else []),
    ]
    offered = {tool.name for tool in request.tools}
    done = _done_refs(request)
    remaining: list[ToolCall] = []
    for call in planned:
        ref = tool_source_ref(call.tool_name, call.args)
        # Only tools the harness offered to this role, and each call at most once per run.
        if call.tool_name in offered and ref not in done:
            remaining.append(call)
            done.add(ref)
    return remaining


def memory_confirmation_done(
    request: InvestigationRequest, familiar_similarity: float
) -> tuple[bool, str]:
    """True (with the reason) once the memory-guided checks ran and live evidence agrees."""
    guide = memory_guide(request, familiar_similarity)
    if guide is None:
        return False, ""
    done = _done_refs(request)
    checks = [_call(check, guide.target) for check in CONFIRMATION_CHECKS[guide.mechanism]]
    if any(tool_source_ref(call.tool_name, call.args) not in done for call in checks):
        return False, ""
    # Only live evidence counts: a memory-origin hypothesis would always "agree" with memory.
    live = generate_hypotheses(list(request.evidence.values()), request.incident)
    if not any(h.mechanism == guide.mechanism and h.target_service == guide.target for h in live):
        return False, ""
    return True, (
        f"memory-guided confirmation consistent with episode {guide.episode_id}: live evidence "
        f"also shows {_words(guide.mechanism)} on {guide.target}"
    )


def memory_guide(request: InvestigationRequest, familiar_similarity: float) -> MemoryGuide | None:
    """The very similar, verified past fix that may steer the investigator, if any."""
    # Memory may only shorten a fresh look. Once the verifier asks for specific services
    # (focus_services), the full playbook runs, so a remembered answer can never stop an
    # escalation round from collecting the evidence the verifier asked for.
    if request.role != AgentRole.INVESTIGATOR or request.focus_services:
        return None
    for hit in request.memory_hints:  # most similar first
        mechanism = hit.episode.failure_mechanism
        if (
            hit.similarity >= familiar_similarity
            and hit.outcome == EpisodeOutcome.SUCCESS
            and hit.episode.verified
            and mechanism is not None
            and mechanism in CONFIRMATION_CHECKS
        ):
            target = memory_target(hit, request.incident)
            return MemoryGuide(hit.episode.episode_id, mechanism, target)
    return None


# --- role playbooks -------------------------------------------------------------------------


def _investigator(request: InvestigationRequest, parsed: ParsedEvidence) -> list[ToolCall]:
    """Full check of each target, then a rollback dry run for the recently released ones."""
    targets = _targets(request, parsed)
    now = parsed.reference_time(request.incident.created_at)
    calls = [_call(check, target) for target in targets for check in INVESTIGATOR_CHECKS]
    calls += [_call(ROLLBACK_PLAN, t) for t in targets if parsed.recent_deployment(t, now)]
    return calls


def _telemetry(request: InvestigationRequest, parsed: ParsedEvidence) -> list[ToolCall]:
    """Health, logs and error rate of each target; memory where crashes or OOM hint at it."""
    targets = _targets(request, parsed)
    calls = [_call(check, target) for target in targets for check in TELEMETRY_CHECKS]
    calls += [_call(MEMORY, t) for t in targets if _memory_suspect(parsed, t)]
    return calls


def _infrastructure(request: InvestigationRequest, parsed: ParsedEvidence) -> list[ToolCall]:
    """Cluster-wide releases and events first, then follow-ups on what they reveal."""
    now = parsed.reference_time(request.incident.created_at)
    released = dict.fromkeys(record.service for _, record in parsed.deployments)
    oom_killed = dict.fromkeys(
        event.service for _, event in parsed.events if event.reason in OOM_EVENT_REASONS
    )
    calls = [_call(check) for check in CLUSTER_CHECKS]
    calls += [_call(ROLLBACK_PLAN, s) for s in released if parsed.recent_deployment(s, now)]
    calls += [_call(HEALTH, s) for s in oom_killed]
    return calls


PLAYBOOKS: dict[AgentRole, Callable[[InvestigationRequest, ParsedEvidence], list[ToolCall]]] = {
    AgentRole.INVESTIGATOR: _investigator,
    AgentRole.TELEMETRY: _telemetry,
    AgentRole.INFRASTRUCTURE: _infrastructure,
}


# --- helpers --------------------------------------------------------------------------------


def _memory_checks(request: InvestigationRequest, familiar_similarity: float) -> list[ToolCall]:
    """The confirmation checks for a remembered mechanism, asked before the normal playbook."""
    guide = memory_guide(request, familiar_similarity)
    if guide is None:
        return []
    prefix = (
        f"confirm past episode {guide.episode_id} ({_words(guide.mechanism)} on {guide.target}): "
    )
    return [_call(check, guide.target, prefix) for check in CONFIRMATION_CHECKS[guide.mechanism]]


def _targets(request: InvestigationRequest, parsed: ParsedEvidence) -> list[str]:
    """Alerted, affected and focus services, then dependencies named in upstream errors."""
    incident = request.incident
    named = [incident.service, *incident.affected_services, *request.focus_services]
    targets = list(dict.fromkeys(named))
    # Appending while iterating follows upstream errors transitively (A -> B -> C), in order.
    for target in targets:
        for dependency in parsed.upstream_failures(target):
            if dependency not in targets:
                targets.append(dependency)
    return targets[:MAX_TARGETS]


def _memory_suspect(parsed: ParsedEvidence, service: str) -> bool:
    """Crash-looping, missing ready replicas, or heap/OOM log lines: memory is worth a look."""
    unhealthy = any(
        snapshot.service == service
        and (snapshot.status == "CrashLoopBackOff" or snapshot.ready_replicas < snapshot.replicas)
        for _, snapshot in parsed.health
    )
    memory_logs = any(
        logs.service == service
        and any(
            is_oom_message(line.message) or _HEAP_WARNING in line.message for line in logs.lines
        )
        for _, logs in parsed.logs
    )
    return unhealthy or memory_logs


def _done_refs(request: InvestigationRequest) -> set[str]:
    """Calls already made: evidence anyone recorded in this run, plus this agent's own history."""
    done = set(request.observed_refs)
    done.update(tool_source_ref(record.tool_name, record.args) for record in request.history)
    return done


def _call(check: Check, service: str | None = None, prefix: str = "") -> ToolCall:
    """Turn a check into a concrete ToolCall (cluster-wide when no service is given)."""
    args = {"service": service, **check.args} if service else dict(check.args)
    reason = prefix + check.question.format(service=service)
    return ToolCall(tool_name=check.tool, args=args, reason=reason)


def _words(mechanism: FailureMechanism) -> str:
    """A mechanism as plain words, e.g. 'bad_deployment' -> 'bad deployment'."""
    return mechanism.value.replace("_", " ")
