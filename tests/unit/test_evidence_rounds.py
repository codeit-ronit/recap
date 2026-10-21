"""Parallel rounds: per-agent evidence IDs, canonical order, and independent agent views.

In a parallel round each specialist sees what was known before the round plus its own
results, never the other specialist's in-flight evidence. IDs and order then do not depend
on which agent finished first, which is what makes a recorded parallel run replayable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from recap.context.evidence import EvidenceRegistry
from recap.context.packer import ContextPacker
from recap.core.models import AgentRole, Incident, RunBudget, SourceType
from recap.harness.budget import BudgetTracker
from recap.harness.loop import AgentHarness
from recap.telemetry.tracer import MemorySink, Tracer

TEL, INFRA, INV = AgentRole.TELEMETRY, AgentRole.INFRASTRUCTURE, AgentRole.INVESTIGATOR


def add(registry: EvidenceRegistry, ref: str, role: AgentRole | None) -> str:
    return registry.add(
        source_type=SourceType.TOOL, source_ref=ref, content=ref, agent_role=role
    ).id


def test_parallel_round_ids_are_per_agent() -> None:
    registry = EvidenceRegistry("run-abc123def0", "inc-1")
    first = add(registry, "mcp:list_services()", INV)
    registry.open_round(2, [TEL, INFRA])
    infra = add(registry, "mcp:get_recent_deployments(service=checkout)", INFRA)
    tel = add(registry, "mcp:get_service_logs(service=checkout)", TEL)
    tel2 = add(registry, "mcp:get_service_metrics(service=checkout)", TEL)
    assert first == "ev-abc123-001"
    assert (tel, tel2, infra) == ("ev-abc123-r2t01", "ev-abc123-r2t02", "ev-abc123-r2i01")


def test_closing_a_round_orders_its_evidence_by_agent_not_arrival() -> None:
    def run(arrival: list[tuple[str, AgentRole]]) -> list[str]:
        registry = EvidenceRegistry("run-abc123def0", "inc-1")
        add(registry, "mcp:list_services()", INV)
        registry.open_round(2, [TEL, INFRA])
        for ref, role in arrival:
            add(registry, ref, role)
        registry.close_round([TEL, INFRA])
        return registry.ids()

    logs, deploys = "mcp:get_service_logs()", "mcp:get_recent_deployments()"
    one = run([(deploys, INFRA), (logs, TEL)])  # infrastructure finished first
    other = run([(logs, TEL), (deploys, INFRA)])  # telemetry finished first
    assert one == other == ["ev-abc123-001", "ev-abc123-r2t01", "ev-abc123-r2i01"]


def test_sequential_ids_stay_unique_after_a_round() -> None:
    registry = EvidenceRegistry("run-abc123def0", "inc-1")
    add(registry, "a", INV)
    registry.open_round(1, [TEL, INFRA])
    add(registry, "b", TEL)
    add(registry, "c", INFRA)
    registry.close_round([TEL, INFRA])
    later = add(registry, "d", INV)  # a later single round numbers sequentially again
    assert later == "ev-abc123-004" and len(set(registry.ids())) == 4


def harness(registry: EvidenceRegistry) -> AgentHarness:
    incident = Incident(
        incident_id="inc-1",
        scenario_id="s",
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        environment="simulated",
        budget=RunBudget(),
    )
    return AgentHarness(
        incident=incident,
        provider=None,  # type: ignore[arg-type]  # visible() needs no provider
        gateway=None,  # type: ignore[arg-type]
        packer=ContextPacker(),
        evidence=registry,
        tracer=Tracer("run-abc123def0", [MemorySink()]),
        budget=BudgetTracker(RunBudget()),
        pack_tokens=3000,
    )


def test_a_specialist_sees_what_was_known_before_the_round_and_its_own_results() -> None:
    registry = EvidenceRegistry("run-abc123def0", "inc-1")
    before = add(registry, "mcp:list_services()", INV)
    h = harness(registry)
    h.round_start = registry.open_round(2, [TEL, INFRA])
    mine = add(registry, "mcp:get_service_logs(service=checkout)", TEL)
    theirs = add(registry, "mcp:get_recent_deployments(service=checkout)", INFRA)

    visible, observed = h.visible(TEL)
    assert [e.id for e in visible] == [before, mine]  # never the other agent's in-round item
    assert "mcp:get_recent_deployments(service=checkout)" not in observed

    registry.close_round([TEL, INFRA])
    h.round_start = None  # after the round, everyone sees everything (and the verifier does)
    assert {e.id for e in h.visible(TEL)[0]} == {before, mine, theirs}
