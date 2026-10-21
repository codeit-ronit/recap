"""Tests for the scripted policy's role playbooks (recap/providers/playbooks.py).

Plans are compared as canonical source refs, the same strings the gateway stamps on the
evidence a call produces, e.g. "mcp:get_service_logs(service=checkout)". Tools offered to
each role come from the real configs (configs/agents.yaml and configs/policies.yaml).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from recap.context.evidence import EvidenceRegistry, tool_source_ref
from recap.core.config import RoutingSettings, Settings
from recap.core.models import (
    ActionRecord,
    AgentRole,
    ContextPack,
    EpisodeOutcome,
    Evidence,
    ExperienceEpisode,
    FailureMechanism,
    Incident,
    InvestigationRequest,
    MemoryHit,
    MemoryPartition,
    RunBudget,
    SourceType,
    ToolCall,
    ToolCallRecord,
    ToolSpecView,
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
    ServiceInfo,
    ServiceList,
)
from recap.core.signals import (
    HEAP_WARNING_TEMPLATE,
    RELEASE_ERROR_TEMPLATE,
    UPSTREAM_ERROR_TEMPLATE,
)
from recap.providers.playbooks import memory_confirmation_done, plan_next_calls

INV, TEL, INFRA = AgentRole.INVESTIGATOR, AgentRole.TELEMETRY, AgentRole.INFRASTRUCTURE
M = FailureMechanism
T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)  # alert time = simulated minute 0
FAMILIAR = RoutingSettings().familiar_similarity  # the default "familiar" memory threshold


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


def checks(service: str) -> list[str]:
    """The investigator's six per-service checks, in playbook order."""
    return [
        f"mcp:get_service_health(service={service})",
        f"mcp:get_service_logs(service={service})",
        f"mcp:get_service_metrics(metric=error_rate,service={service})",
        f"mcp:get_recent_deployments(service={service})",
        f"mcp:get_k8s_like_events(service={service})",
        f"mcp:get_service_metrics(metric=memory_mb,service={service})",
    ]


def without(refs: list[str], *dropped: str) -> list[str]:
    return [ref for ref in refs if ref not in dropped]


def refs(calls: list[ToolCall]) -> list[str]:
    return [tool_source_ref(call.tool_name, call.args) for call in calls]


def role_tools(settings: Settings, role: AgentRole) -> list[ToolSpecView]:
    """The read-only tools the harness offers this role (from the real configs)."""
    policies = settings.policies.tools
    return [
        ToolSpecView(name=name, description=policies[name].description, risk=policies[name].risk)
        for name in settings.agents[role].tools
    ]


def make_request(
    settings: Settings,
    role: AgentRole = INV,
    *,
    evidence: list[Evidence] | None = None,
    observed: list[str] | None = None,
    history: list[ToolCallRecord] | None = None,
    memory: list[MemoryHit] | None = None,
    focus: list[str] | None = None,
    affected: list[str] | None = None,
) -> InvestigationRequest:
    """One provider request: the run's evidence counts as observed, like in the harness."""
    evidence = evidence or []
    incident = Incident(
        incident_id="inc-1",
        scenario_id="unit",
        service="checkout",
        affected_services=affected or ["checkout"],
        symptoms=["checkout: HTTP 5xx ratio above 30% (SLO 1%)"],
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )
    return InvestigationRequest(
        role=role,
        goal=settings.agents[role].goal,
        incident=incident,
        context=ContextPack(budget_tokens=3000, tokens_before=0, tokens_after=0),
        evidence={item.id: item for item in evidence},
        tools=role_tools(settings, role),
        history=history or [],
        observed_refs=[item.source_ref for item in evidence] + (observed or []),
        step=1,
        max_steps=10,
        memory_hints=memory or [],
        focus_services=focus or [],
    )


class Recorder:
    """Records observation models as tool evidence, exactly like the gateway does."""

    def __init__(self) -> None:
        self.registry = EvidenceRegistry("run-unit02", "inc-1")

    def add(self, tool: str, observation: BaseModel, **args: Any) -> Evidence:
        return self.registry.add(
            source_type=SourceType.TOOL,
            source_ref=tool_source_ref(tool, args),
            content=observation.model_dump_json(),
            data=observation.model_dump(mode="json"),
            tool_name=tool,
        )

    def release(self, name: str, minute: float, **query: Any) -> Evidence:
        """A v2.0.0 release of `name`, as returned by a deployments query with `query` args."""
        record = DeploymentRecord(
            service=name, version="v2.0.0", previous_version="v1.9.0", deployed_at=at(minute)
        )
        return self.add("get_recent_deployments", DeploymentList(deployments=[record]), **query)

    def logs(self, service: str, *messages: tuple[Any, str]) -> Evidence:
        lines = [
            LogLine(timestamp=at(-len(messages) + i), level=level, service=service, message=text)
            for i, (level, text) in enumerate(messages)
        ]
        result = LogQueryResult(service=service, lines=lines, total_matched=len(lines))
        return self.add("get_service_logs", result, service=service)

    def error_rate(self, service: str, values: list[float]) -> Evidence:
        points = [
            MetricPoint(timestamp=at(5 * (i + 1 - len(values))), value=v)
            for i, v in enumerate(values)
        ]
        result = MetricSeries(service=service, metric="error_rate", unit="ratio", points=points)
        return self.add("get_service_metrics", result, service=service, metric="error_rate")

    def health(self, service: str, status: Any = "Running", ready: int = 2) -> Evidence:
        snapshot = HealthSnapshot(
            service=service,
            status=status,
            ready_replicas=ready,
            replicas=2,
            error_rate=0.2,
            latency_p95_ms=90.0,
            observed_at=at(0),
        )
        return self.add("get_service_health", snapshot, service=service)

    def events(self, *items: tuple[str, str], **query: Any) -> Evidence:
        found = [
            ClusterEvent(
                timestamp=at(-1),
                type="Warning",
                reason=reason,
                object=f"pod/{service}-x2k9q",
                service=service,
                message=reason,
            )
            for service, reason in items
        ]
        return self.add("get_k8s_like_events", EventList(events=found), **query)

    @property
    def evidence(self) -> list[Evidence]:
        return self.registry.all()


def upstream(dependency: str) -> tuple[str, str]:
    return ("ERROR", UPSTREAM_ERROR_TEMPLATE.format(dependency=dependency, reason="timeout"))


def memory_hit(
    mechanism: FailureMechanism = M.BAD_DEPLOYMENT,
    target: str = "checkout",
    similarity: float = 0.9,
    outcome: EpisodeOutcome = EpisodeOutcome.SUCCESS,
) -> MemoryHit:
    success = outcome == EpisodeOutcome.SUCCESS
    episode = ExperienceEpisode(
        episode_id="ep-1",
        origin_run_id="run-old",
        scenario_id="earlier",
        failure_family=mechanism.value,
        partition=MemoryPartition.DEMO,
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        failure_mechanism=mechanism,
        mechanism_target=target,
        successful_action=(
            ActionRecord(action="execute_rollback", target=target, result="recovered")
            if success
            else None
        ),
        outcome=outcome,
        verified=success,
        created_at=at(-5000),
    )
    return MemoryHit(episode=episode, similarity=similarity, outcome=outcome)


# --- the investigator playbook --------------------------------------------------------------


def test_investigator_checks_the_alerted_service_in_playbook_order(settings: Settings) -> None:
    plan = plan_next_calls(make_request(settings), FAMILIAR)

    assert refs(plan) == checks("checkout")
    assert all(call.reason for call in plan)


def test_recent_release_adds_a_rollback_dry_run_after_the_checks(settings: Settings) -> None:
    recent = Recorder()
    deploys = recent.release("checkout", -12, service="checkout")
    plan = plan_next_calls(make_request(settings, evidence=recent.evidence), FAMILIAR)
    assert refs(plan) == [
        *without(checks("checkout"), deploys.source_ref),
        "mcp:propose_rollback(service=checkout)",
    ]

    old = Recorder()
    old.release("checkout", -2880, service="checkout")
    plan = plan_next_calls(make_request(settings, evidence=old.evidence), FAMILIAR)
    assert "mcp:propose_rollback(service=checkout)" not in refs(plan)


def test_upstream_errors_add_dependencies_after_the_primary_targets(settings: Settings) -> None:
    recorder = Recorder()
    checkout_logs = recorder.logs("checkout", upstream("payment"), upstream("payment"))
    payment_logs = recorder.logs("payment", upstream("inventory"))  # followed transitively

    plan = plan_next_calls(make_request(settings, evidence=recorder.evidence), FAMILIAR)

    assert refs(plan) == [
        *without(checks("checkout"), checkout_logs.source_ref),
        *without(checks("payment"), payment_logs.source_ref),
        *checks("inventory"),
    ]


def test_calls_already_made_are_never_planned_again(settings: Settings) -> None:
    by_other_agent = "mcp:get_service_health(service=checkout)"
    failed = ToolCallRecord(
        tool_name="get_service_logs", args={"service": "checkout"}, ok=False, error="timeout"
    )

    plan = plan_next_calls(
        make_request(settings, observed=[by_other_agent], history=[failed]), FAMILIAR
    )

    assert refs(plan) == without(
        checks("checkout"), by_other_agent, "mcp:get_service_logs(service=checkout)"
    )


def test_targets_are_capped_at_three_services(settings: Settings) -> None:
    affected = ["checkout", "frontend", "cart"]
    for role, per_service in ((INV, 6), (TEL, 2)):  # telemetry: logs and error rate
        plan = plan_next_calls(
            make_request(settings, role, affected=affected, focus=["payment"]), FAMILIAR
        )
        assert [call.args["service"] for call in plan] == [
            service for service in affected for _ in range(per_service)
        ]


# --- the specialist playbooks ---------------------------------------------------------------


def test_infrastructure_starts_cluster_wide_then_follows_up(settings: Settings) -> None:
    fresh = plan_next_calls(make_request(settings, INFRA), FAMILIAR)
    assert refs(fresh) == [
        "mcp:list_services()",
        "mcp:get_recent_deployments(limit=10)",
        "mcp:get_k8s_like_events(limit=30)",
    ]

    recorder = Recorder()
    services = [ServiceInfo(name="payment", namespace="shop", version="v2.0.0", replicas=2)]
    recorder.add("list_services", ServiceList(namespace="shop", services=services))
    recorder.release("payment", -15, limit=10)
    recorder.release("cart", -3000, limit=10)
    recorder.events(("inventory", "OOMKilled"), ("inventory", "BackOff"), limit=30)

    plan = plan_next_calls(make_request(settings, INFRA, evidence=recorder.evidence), FAMILIAR)

    assert refs(plan) == [
        "mcp:propose_rollback(service=payment)",
        "mcp:get_service_health(service=inventory)",
    ]


def test_telemetry_reads_memory_only_when_health_or_logs_hint_at_it(settings: Settings) -> None:
    telemetry_checks = checks("checkout")[1:3]  # logs and error rate (health is infrastructure's)
    memory = "mcp:get_service_metrics(metric=memory_mb,service=checkout)"
    assert refs(plan_next_calls(make_request(settings, TEL), FAMILIAR)) == telemetry_checks

    crashing = Recorder()  # health evidence another agent recorded before this round
    crashing.health("checkout", status="CrashLoopBackOff", ready=1)
    plan = refs(plan_next_calls(make_request(settings, TEL, evidence=crashing.evidence), FAMILIAR))
    assert plan == [*telemetry_checks, memory]

    heap = Recorder()
    heap.logs("checkout", ("WARN", HEAP_WARNING_TEMPLATE.format(percent=93)))
    plan = refs(plan_next_calls(make_request(settings, TEL, evidence=heap.evidence), FAMILIAR))
    assert plan[-1] == memory


def test_roles_only_plan_tools_they_are_offered(settings: Settings) -> None:
    for role in (INV, TEL, INFRA):
        request = make_request(settings, role, memory=[memory_hit()])
        offered = {tool.name for tool in request.tools}
        assert {call.tool_name for call in plan_next_calls(request, FAMILIAR)} <= offered
    # A role with no tools offered has nothing to plan.
    bare = make_request(settings).model_copy(update={"tools": []})
    assert plan_next_calls(bare, FAMILIAR) == []


# --- memory-guided confirmation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("mechanism", "confirmation"),
    [
        (
            M.BAD_DEPLOYMENT,
            [
                "mcp:get_recent_deployments(service=payment)",
                "mcp:get_service_metrics(metric=error_rate,service=payment)",
                "mcp:get_service_logs(service=payment)",
            ],
        ),
        (
            M.RESOURCE_PRESSURE,
            [
                "mcp:get_k8s_like_events(service=payment)",
                "mcp:get_service_metrics(metric=memory_mb,service=payment)",
                "mcp:get_service_logs(service=payment)",
            ],
        ),
    ],
)
def test_memory_guidance_runs_the_confirmation_checks_first(
    settings: Settings, mechanism: FailureMechanism, confirmation: list[str]
) -> None:
    hit = memory_hit(mechanism, target="payment")  # payment is part of this incident
    request = make_request(settings, affected=["checkout", "payment"], memory=[hit])

    plan = plan_next_calls(request, FAMILIAR)

    assert refs(plan)[:3] == confirmation
    assert all("past episode ep-1" in call.reason for call in plan[:3])
    # Then the normal playbook continues, without repeating the confirmation checks.
    assert refs(plan)[3:] == [*checks("checkout"), *without(checks("payment"), *confirmation)]


def test_the_familiar_threshold_comes_from_the_caller(settings: Settings) -> None:
    # Regression: it was a literal 0.6, so configs/budget.yaml could not change the shortcut.
    request = make_request(settings, memory=[memory_hit(similarity=0.7)])

    assert refs(plan_next_calls(request, 0.6))[0] == "mcp:get_recent_deployments(service=checkout)"
    assert refs(plan_next_calls(request, 0.8))[0] == "mcp:get_service_health(service=checkout)"


def test_memory_target_falls_back_to_the_alerted_service(settings: Settings) -> None:
    hit = memory_hit(M.BAD_DEPLOYMENT, target="inventory")  # not part of this incident
    plan = plan_next_calls(make_request(settings, memory=[hit]), FAMILIAR)
    assert refs(plan)[0] == "mcp:get_recent_deployments(service=checkout)"


@pytest.mark.parametrize(
    "case",
    ["not similar enough", "failed outcome", "not actionable", "specialist role", "focus"],
)
def test_memory_guidance_needs_a_close_verified_fix_and_a_fresh_look(
    settings: Settings, case: str
) -> None:
    hit = {
        "not similar enough": memory_hit(similarity=0.5),
        "failed outcome": memory_hit(outcome=EpisodeOutcome.FAILED),
        "not actionable": memory_hit(M.DEPENDENCY_FAILURE),
    }.get(case, memory_hit())
    role = TEL if case == "specialist role" else INV
    focus = ["checkout"] if case == "focus" else None  # the verifier asked for evidence

    request = make_request(settings, role, memory=[hit], focus=focus)

    # No memory shortcut: the role's normal checklist starts (telemetry's starts with logs).
    first = "get_service_logs" if role == TEL else "get_service_health"
    assert refs(plan_next_calls(request, FAMILIAR))[0] == f"mcp:{first}(service=checkout)"
    assert memory_confirmation_done(request, FAMILIAR) == (False, "")


def test_memory_confirmation_is_done_only_when_live_evidence_agrees(settings: Settings) -> None:
    hit = memory_hit(M.BAD_DEPLOYMENT, target="checkout")
    assert memory_confirmation_done(make_request(settings, memory=[hit]), FAMILIAR) == (False, "")

    def confirmation_evidence(release_minute: float, with_logs: bool = True) -> list[Evidence]:
        recorder = Recorder()
        recorder.release("checkout", release_minute, service="checkout")
        recorder.error_rate("checkout", [0.0, 0.0, 0.31, 0.33])
        if with_logs:
            message = RELEASE_ERROR_TEMPLATE.format(exception="KeyError", version="v2.0.0")
            recorder.logs("checkout", ("ERROR", message))
        return recorder.evidence

    agreeing = make_request(settings, memory=[hit], evidence=confirmation_evidence(-12))
    done, reason = memory_confirmation_done(agreeing, FAMILIAR)
    assert done
    assert "episode ep-1" in reason and "bad deployment on checkout" in reason
    # Above the hit's similarity (0.9) the episode is not familiar, so there is no shortcut.
    assert memory_confirmation_done(agreeing, 0.95) == (False, "")

    # Only two of the three checks have run so far: keep going.
    partial = make_request(settings, memory=[hit], evidence=confirmation_evidence(-12, False))
    assert memory_confirmation_done(partial, FAMILIAR) == (False, "")

    # The release is old, so live evidence does not show a bad deployment: keep investigating.
    disagreeing = make_request(settings, memory=[hit], evidence=confirmation_evidence(-2880))
    assert memory_confirmation_done(disagreeing, FAMILIAR) == (False, "")
    assert (
        refs(plan_next_calls(disagreeing, FAMILIAR))[0]
        == "mcp:get_service_health(service=checkout)"
    )
