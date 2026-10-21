"""The specialists' contracts (configs/agents.yaml) and the extended finding contract.

Each specialist sees different things, so each may conclude only what it can observe: the
telemetry investigator (logs, metrics) cannot claim a bad release, because it cannot see
deployments. The contract is a constraint for the model (the schema's enum) and a rule for
every provider (the harness drops an out-of-role claim). A finding also says how the agent
ended and carries the contradicting evidence and the model's own confidence note, which is
untrusted text that nothing decides on.
"""

from __future__ import annotations

import json

import pytest

from recap.context.evidence import EvidenceRegistry
from recap.context.packer import ContextPacker
from recap.core.config import Settings
from recap.core.models import (
    AgentRole,
    FailureMechanism,
    Hypothesis,
    InvestigationDecision,
    InvestigationRequest,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    RunBudget,
    ToolCall,
)
from recap.harness.budget import BudgetTracker
from recap.harness.loop import AgentHarness
from recap.providers import protocol
from recap.routing.orchestrator import AgentRunPlan
from recap.telemetry.tracer import MemorySink, TraceCategory, Tracer
from tests.llm_fakes import evidence, hypothesis, investigation_request, step
from tests.unit.test_harness_loop import INCIDENT, FakeGateway, FakeTools

TEL, INFRA, INV = AgentRole.TELEMETRY, AgentRole.INFRASTRUCTURE, AgentRole.INVESTIGATOR
LOGS = evidence("ev-abc123-001", "mcp:get_service_logs(service=checkout)", "ERROR NPE")
METRICS = evidence("ev-abc123-002", "mcp:get_service_metrics(service=checkout)", "errors 0.32")


def test_the_specialists_share_no_environment_tool(settings: Settings) -> None:
    telemetry, infrastructure = settings.agents[TEL], settings.agents[INFRA]

    def environment_tools(tools: list[str]) -> set[str]:  # search_runbooks is RECAP's own
        return {t for t in tools if settings.policies.tools[t].source == "mcp"}

    assert environment_tools(telemetry.tools).isdisjoint(environment_tools(infrastructure.tools))
    assert "search_runbooks" in telemetry.tools and "search_runbooks" in infrastructure.tools
    assert "get_recent_deployments" not in telemetry.tools  # it cannot see releases ...
    assert FailureMechanism.BAD_DEPLOYMENT not in telemetry.mechanisms  # ... nor claim one
    assert FailureMechanism.BAD_DEPLOYMENT in infrastructure.mechanisms
    assert telemetry.responsibilities and infrastructure.responsibilities


def test_a_role_is_shown_its_contract_and_offered_only_its_mechanisms(settings: Settings) -> None:
    request = investigation_request(settings, role=TEL, seen=[LOGS])
    text = protocol.render_investigation(request, "json")
    assert "You may conclude only: resource_pressure, dependency_failure, unknown" in text
    assert settings.agents[TEL].responsibilities[0] in text
    for schema in (
        protocol.step_schema(request),
        protocol.tool_definitions(request)[-1]["function"]["parameters"],  # submit_findings
    ):
        found = json.dumps(schema)
        assert '"bad_deployment"' not in found and '"resource_pressure"' in found
    # The single investigator keeps every mechanism.
    assert '"bad_deployment"' in json.dumps(protocol.step_schema(investigation_request(settings)))


def test_an_out_of_role_claim_is_dropped_with_a_note(settings: Settings) -> None:
    request = investigation_request(settings, role=TEL, seen=[LOGS])
    answer = step("hypothesis", hypotheses=[hypothesis("checkout", [LOGS.id])])  # bad_deployment
    parsed = protocol.parse_json_step(answer["content"], request)
    assert parsed.decision is None  # nothing usable is left: one corrective retry follows
    assert any("may not make" in note for note in parsed.notes)


def test_a_finding_carries_contradicting_evidence_and_the_models_own_note(
    settings: Settings,
) -> None:
    request = investigation_request(settings, role=TEL, seen=[LOGS, METRICS])
    claim = hypothesis(
        "checkout",
        [LOGS.id],
        mechanism="resource_pressure",
        version=None,
        contradicting=[METRICS.id, "ev-abc123-999"],  # the second ID does not exist
        confidence_note="Fairly sure \x1b[31mbut memory is flat",
    )
    parsed = protocol.parse_json_step(step("hypothesis", hypotheses=[claim])["content"], request)
    assert isinstance(parsed.decision, InvestigationDecision)
    assert parsed.decision.kind == "hypothesis"
    [made] = parsed.decision.hypotheses
    assert made.contradicting_evidence_ids == [METRICS.id]  # unknown IDs are dropped
    assert made.confidence_note == "Fairly sure [31mbut memory is flat"  # no escape codes


# --- the harness enforces the contract for every provider, and records how agents end ----


class OneAnswer:
    """A provider that always gives the same decision (stands in for any provider)."""

    info = ProviderInfo(name="one-answer", kind="scripted", origin="scripted")

    def __init__(self, decision: InvestigationDecision) -> None:
        self.decision = decision

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        return self.decision

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        return ProposalDecision()


async def run_agent(settings: Settings, role: AgentRole, decision: InvestigationDecision):  # type: ignore[no-untyped-def]
    registry = EvidenceRegistry("run-roles000001", INCIDENT.incident_id)
    sink = MemorySink()
    harness = AgentHarness(
        incident=INCIDENT,
        provider=OneAnswer(decision),
        gateway=FakeGateway(FakeTools(registry)),  # type: ignore[arg-type]
        packer=ContextPacker(),
        evidence=registry,
        tracer=Tracer("run-roles000001", [sink]),
        budget=BudgetTracker(RunBudget()),
        pack_tokens=3000,
    )
    plan = AgentRunPlan(role=role, round=1, max_steps=3)
    finding = await harness.run_agent(settings.agents[role], plan)
    [finished] = [e for e in sink.events if e.category == TraceCategory.AGENT_FINISH]
    return finding, finished


async def test_the_harness_drops_an_out_of_role_claim_from_any_provider(
    settings: Settings,
) -> None:
    claims = [
        Hypothesis(claim="a bad release", mechanism=FailureMechanism.BAD_DEPLOYMENT),
        Hypothesis(claim="memory pressure", mechanism=FailureMechanism.RESOURCE_PRESSURE),
    ]
    decision = InvestigationDecision(action="finish", hypotheses=claims, kind="hypothesis")
    finding, finished = await run_agent(settings, TEL, decision)
    assert [h.mechanism for h in finding.hypotheses] == [FailureMechanism.RESOURCE_PRESSURE]
    assert finished.payload["dropped_out_of_role"] == ["bad_deployment"]
    # The single investigator may make both claims.
    finding, _ = await run_agent(settings, INV, decision)
    assert len(finding.hypotheses) == 2


@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        (InvestigationDecision(action="finish", kind="need_more_evidence"), "need_more_evidence"),
        (InvestigationDecision(action="finish", kind="stop"), "stop"),
        (InvestigationDecision(action="finish", kind="no_usable_answer"), "no_usable_answer"),
        (
            InvestigationDecision(
                action="call_tools",
                tool_calls=[ToolCall(tool_name="get_service_health", args={"service": "checkout"})],
            ),
            "step_limit",
        ),
    ],
    ids=["needs-more", "stop", "no-usable-answer", "step-limit"],
)
async def test_a_finding_says_how_the_agent_ended(
    settings: Settings, decision: InvestigationDecision, outcome: str
) -> None:
    finding, finished = await run_agent(settings, INV, decision)
    assert finding.outcome == outcome == finished.payload["outcome"]
