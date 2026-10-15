"""Agent harness: the observe -> reason -> act loop that every investigator runs.

One loop iteration ("step"):
  1. observe  - build a bounded context pack from the evidence registry, docs and memory
  2. reason   - ask the provider (scripted policy or LLM) for the next decision
  3. act      - run the requested read-only tool calls through the safety gateway

The loop stops when the provider finishes, when the agent's step allocation is used
up, or when the run budget is exhausted (BudgetExhausted propagates to the controller).
The harness is provider-agnostic: it never knows whether the "brain" is an LLM.

What a provider sees is built here, so this is also where the no-leak rule is applied:
the incident and the remembered episodes are converted to their label-free views
(AgentIncident, AgentMemoryHit) before they reach the packer or the provider request.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from recap.context.evidence import EvidenceRegistry
from recap.core.config import AgentSpec
from recap.core.ids import utcnow
from recap.core.interfaces import LLMProvider
from recap.core.models import (
    AgentFinding,
    Incident,
    InvestigationRequest,
    MemoryHit,
    RetrievedChunk,
    ToolCallRecord,
    Usage,
)
from recap.harness.budget import BudgetTracker
from recap.telemetry.tracer import TraceCategory, Tracer

if TYPE_CHECKING:
    from recap.context.packer import ContextPacker
    from recap.routing.orchestrator import AgentRunPlan
    from recap.tools.gateway import ToolGateway

# A provider may ask for several calls in one step; the harness caps it to keep
# timelines readable and budgets predictable.
MAX_CALLS_PER_STEP = 2


class AgentHarness:
    """Runs agents for one incident. Shared by all agents of a run."""

    def __init__(
        self,
        *,
        incident: Incident,
        provider: LLMProvider,
        gateway: ToolGateway,
        packer: ContextPacker,
        evidence: EvidenceRegistry,
        tracer: Tracer,
        budget: BudgetTracker,
        pack_tokens: int,
    ) -> None:
        self.incident = incident
        # What the packer and providers see: the incident without its evaluation labels.
        self.agent_incident = incident.agent_view()
        self.provider = provider
        self.gateway = gateway
        self.packer = packer
        self.evidence = evidence
        self.tracer = tracer
        self.budget = budget
        self.pack_tokens = pack_tokens
        # Retrieved knowledge visible to agents; the controller fills these in.
        self.docs: Sequence[RetrievedChunk] = ()
        self.memory_hits: Sequence[MemoryHit] = ()
        # Evidence the packer must keep (e.g. evidence contradicting a hypothesis).
        self.priority_ids: Sequence[str] = ()
        # Called with each decision's usage the moment it is produced, so cost is counted
        # even when a round later ends in a timeout, a crash or BudgetExhausted.
        self.on_usage: Callable[[Usage], None] = lambda usage: None
        # Evidence recorded before this index belongs to an earlier "epoch" (before a
        # mitigation changed the system), so agents may observe those things again.
        self.epoch_start = 0

    async def run_agent(self, spec: AgentSpec, plan: AgentRunPlan) -> AgentFinding:
        """Run one agent until it finishes or its step allocation is used up."""
        role = spec.role
        started = utcnow()
        tools = self.gateway.read_only_view(role, spec.tools)
        history: list[ToolCallRecord] = []
        collected: list[str] = []
        doc_refs: list[str] = []  # runbook chunks this agent saw: its citations
        usage = Usage()
        self.tracer.emit(
            TraceCategory.AGENT_START,
            {
                "role": role.value,
                "round": plan.round,
                "max_steps": plan.max_steps,
                "focus_services": plan.focus_services,
                "tools": spec.tools,
            },
            agent=role.value,
        )

        final = None
        for step in range(1, plan.max_steps + 1):
            self.budget.charge_step()
            # The controller may re-assess memory between rounds, so take fresh views each step.
            memories = [hit.agent_view() for hit in self.memory_hits]

            # 1. observe: a bounded view of everything recorded so far. The pack is never
            # shrunk to fit the run budget: when the budget cannot pay for a normal pack,
            # charge_tokens raises BudgetExhausted and the run stops honestly.
            pack = self.packer.build(
                incident=self.agent_incident,
                evidence=self.evidence.all(),
                docs=self.docs,
                memories=memories,
                max_tokens=self.pack_tokens,
                focus_services=plan.focus_services,
                priority_ids=self.priority_ids,
            )
            self.budget.charge_tokens(pack.tokens_after)
            doc_refs += [ref for ref in pack.ids("doc") if ref not in doc_refs]
            self.tracer.emit(
                TraceCategory.CONTEXT,
                {
                    "step": step,
                    "tokens_before": pack.tokens_before,
                    "tokens_after": pack.tokens_after,
                    "budget_tokens": pack.budget_tokens,
                    "items": pack.ids(),
                    "dropped": pack.dropped_ids,
                    "estimate": True,
                },
                agent=role.value,
            )

            # 2. reason: the provider only sees packed evidence
            request = InvestigationRequest(
                role=role,
                goal=spec.goal,
                incident=self.agent_incident,
                context=pack,
                evidence={e.id: e for e in self.evidence.by_ids(pack.ids("evidence"))},
                tools=tools.specs(),
                history=history,
                observed_refs=[e.source_ref for e in self.evidence.all()[self.epoch_start :]],
                step=step,
                max_steps=plan.max_steps,
                memory_hints=memories,
                focus_services=plan.focus_services,
            )
            decision = await self.provider.next_action(request)
            usage = usage.plus(decision.usage)
            self.on_usage(decision.usage)
            self.tracer.emit(
                TraceCategory.AGENT_DECISION,
                {
                    "step": step,
                    "action": decision.action,
                    "tool_calls": [c.model_dump() for c in decision.tool_calls],
                    "summary": decision.summary,
                    "hypotheses": [h.claim for h in decision.hypotheses],
                },
                agent=role.value,
                usage=decision.usage,
            )
            if decision.action == "finish" or step == plan.max_steps:
                final = decision
                break

            # 3. act: read-only calls through the gateway (workers cannot write)
            for call in decision.tool_calls[:MAX_CALLS_PER_STEP]:
                result = await tools.call(call.tool_name, call.args)
                history.append(
                    ToolCallRecord(
                        tool_name=call.tool_name,
                        args=call.args,
                        ok=result.ok,
                        evidence_id=result.evidence_id,
                        error=result.error,
                    )
                )
                if result.evidence_id:
                    collected.append(result.evidence_id)

        hypotheses = final.hypotheses if final and final.action == "finish" else []
        summary = final.summary if final else ""
        if final and final.action != "finish":
            summary = (summary + " (step limit reached before the agent finished)").strip()
        finding = AgentFinding(
            agent_role=role,
            evidence_ids=collected,
            doc_refs=doc_refs,
            hypotheses=[h.model_copy(update={"proposed_by": role}) for h in hypotheses],
            proposed_next_steps=final.next_steps if final else [],
            usage=usage.plus(Usage(tool_calls=len(history))),
            summary=summary,
            started_at=started,
            finished_at=utcnow(),
            round=plan.round,
        )
        self.tracer.emit(
            TraceCategory.AGENT_FINISH,
            {
                "role": role.value,
                "round": plan.round,
                "steps": usage.steps,
                "tool_calls": len(history),
                "evidence_ids": collected,
                "hypotheses": [h.claim for h in finding.hypotheses],
                "summary": summary,
            },
            agent=role.value,
            usage=finding.usage,
        )
        return finding
