"""Unit tests for the agent harness loop (observe -> reason -> act) with fake parts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from recap.context.evidence import EvidenceRegistry
from recap.context.packer import ContextPacker
from recap.core.config import AgentSpec
from recap.core.models import (
    AgentRole,
    DocChunk,
    FailureMechanism,
    Hypothesis,
    Incident,
    InvestigationDecision,
    InvestigationRequest,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    RetrievedChunk,
    RunBudget,
    SourceType,
    ToolCall,
    ToolResult,
    ToolRisk,
    ToolSpecView,
    Usage,
)
from recap.harness.budget import BudgetTracker
from recap.harness.loop import MAX_CALLS_PER_STEP, AgentHarness
from recap.routing.orchestrator import AgentRunPlan
from recap.telemetry.tracer import MemorySink, TraceCategory, Tracer

INCIDENT = Incident(
    incident_id="inc-1",
    scenario_id="s",
    service="checkout",
    symptoms=["checkout: HTTP 5xx ratio above 30%"],
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
    environment="simulated",
    budget=RunBudget(),
)
SPEC = AgentSpec(
    role=AgentRole.INVESTIGATOR, goal="find it", tools=["get_service_health"], max_steps=5
)


class FakeTools:
    """Stands in for the gateway's ReadOnlyTools: records calls, makes one evidence each."""

    def __init__(self, evidence: EvidenceRegistry) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def specs(self) -> list[ToolSpecView]:
        return [ToolSpecView(name="get_service_health", description="", risk=ToolRisk.READ)]

    async def call(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append((tool_name, args))
        ev = self.evidence.add(
            source_type=SourceType.TOOL, source_ref=f"mcp:{tool_name}", content="ok"
        )
        return ToolResult(invocation_id="c", tool_name=tool_name, ok=True, evidence_id=ev.id)


class FakeGateway:
    def __init__(self, tools: FakeTools) -> None:
        self.tools = tools

    def read_only_view(self, caller: AgentRole, allowed_tools: list[str]) -> FakeTools:
        return self.tools


class GreedyProvider:
    """Always asks for three tool calls; finishes only when told to."""

    info = ProviderInfo(name="greedy", kind="scripted")

    def __init__(self, finish_at: int | None = None) -> None:
        self.finish_at = finish_at
        self.requests: list[InvestigationRequest] = []

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        self.requests.append(request)
        usage = Usage(steps=1, estimated_tokens=10)
        if self.finish_at is not None and request.step >= self.finish_at:
            claim = Hypothesis(claim="bad release", mechanism=FailureMechanism.BAD_DEPLOYMENT)
            return InvestigationDecision(action="finish", hypotheses=[claim], usage=usage)
        call = ToolCall(tool_name="get_service_health", args={"service": "checkout"})
        return InvestigationDecision(action="call_tools", tool_calls=[call] * 3, usage=usage)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        return ProposalDecision()


def make_harness(
    provider: GreedyProvider,
) -> tuple[AgentHarness, FakeTools, MemorySink, list[Usage]]:
    evidence = EvidenceRegistry("run-loop000001", INCIDENT.incident_id)
    tools = FakeTools(evidence)
    sink = MemorySink()
    harness = AgentHarness(
        incident=INCIDENT,
        provider=provider,
        gateway=FakeGateway(tools),  # type: ignore[arg-type]
        packer=ContextPacker(),
        evidence=evidence,
        tracer=Tracer("run-loop000001", [sink]),
        budget=BudgetTracker(RunBudget()),
        pack_tokens=3000,
    )
    seen: list[Usage] = []
    harness.on_usage = seen.append
    chunk = DocChunk(
        chunk_id="runbook#remediation-1",
        doc_path="r.md",
        title="R",
        heading="H",
        text="Mechanism: bad_deployment",
        checksum="x",
    )
    harness.docs = [RetrievedChunk(chunk=chunk, score=1.0, rank=1, retriever="bm25")]
    return harness, tools, sink, seen


async def test_step_cap_and_per_step_call_cap() -> None:
    harness, tools, sink, seen = make_harness(GreedyProvider())
    finding = await harness.run_agent(SPEC, AgentRunPlan(role=SPEC.role, round=1, max_steps=3))

    # Steps 1 and 2 act (at most MAX_CALLS_PER_STEP calls each); step 3 only decides.
    assert len(tools.calls) == 2 * MAX_CALLS_PER_STEP
    assert finding.usage.steps == 3 and "step limit reached" in finding.summary
    assert finding.hypotheses == []
    assert len(seen) == 3  # usage is reported the moment each decision is made
    contexts = [e for e in sink.events if e.category == TraceCategory.CONTEXT]
    assert len(contexts) == 3 and all(e.payload["estimate"] for e in contexts)


async def test_finish_returns_hypotheses_and_citations() -> None:
    provider = GreedyProvider(finish_at=2)
    harness, tools, _, _ = make_harness(provider)
    finding = await harness.run_agent(SPEC, AgentRunPlan(role=SPEC.role, round=1, max_steps=5))

    assert len(tools.calls) == MAX_CALLS_PER_STEP  # only step 1 acted
    assert finding.hypotheses[0].proposed_by == AgentRole.INVESTIGATOR
    assert finding.doc_refs == ["runbook#remediation-1"]  # the runbook chunk it saw
    assert finding.evidence_ids and len(finding.evidence_ids) == MAX_CALLS_PER_STEP
    # The second request carried the history of the first step's calls.
    assert len(provider.requests[1].history) == MAX_CALLS_PER_STEP
