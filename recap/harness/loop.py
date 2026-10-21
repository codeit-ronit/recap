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
from recap.core.errors import model_calls_of
from recap.core.ids import utcnow
from recap.core.interfaces import LLMProvider
from recap.core.models import (
    AgentFinding,
    AgentRole,
    Evidence,
    Hypothesis,
    Incident,
    InvestigationDecision,
    InvestigationRequest,
    MemoryHit,
    ModelCall,
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
        # During a parallel round: where the round's evidence starts. Each specialist then
        # sees what was known before the round plus its own results, never the other
        # specialist's in-flight evidence (independent views; replays that do not depend on
        # which agent finished first). The verifier still reads all of it.
        self.round_start: int | None = None

    def visible(self, role: AgentRole) -> tuple[list[Evidence], list[str]]:
        """The evidence an agent may see now, and the tool calls it counts as already made
        (this epoch's source refs among them)."""
        items = self.evidence.all()
        start = self.round_start
        shown = [start is None or i < start or e.agent_role == role for i, e in enumerate(items)]
        visible = [e for e, keep in zip(items, shown, strict=True) if keep]
        observed = [
            e.source_ref
            for i, (e, keep) in enumerate(zip(items, shown, strict=True))
            if keep and i >= self.epoch_start
        ]
        return visible, observed

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
                "questions": plan.questions,
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
            visible, observed = self.visible(role)
            pack = self.packer.build(
                incident=self.agent_incident,
                evidence=visible,
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
                observed_refs=observed,
                step=step,
                max_steps=plan.max_steps,
                memory_hints=memories,
                focus_services=plan.focus_services,
                responsibilities=spec.responsibilities,
                mechanisms=spec.mechanisms,
                open_questions=plan.questions,
            )
            try:
                decision = await self.provider.next_action(request)
            except Exception as exc:
                # A real model may have answered before the decision failed (a refusal, a
                # quota stop, the free-plan tripwire): record those requests, then go on.
                calls, spent = model_calls_of(exc)
                self._trace_model_calls(calls, step, role.value)
                if spent is not None:
                    self.on_usage(spent)
                raise
            usage = usage.plus(decision.usage)
            self.on_usage(decision.usage)
            # A real model's requests come first, so the timeline shows inference before
            # the decision it produced (the scripted policy sends none).
            self._trace_model_calls(decision.model_calls, step, role.value)
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
        # The role's contract holds for every provider: a claim outside the mechanisms this
        # role may conclude is dropped (a scripted policy or a model could still make one).
        allowed = set(spec.mechanisms)
        dropped = [h for h in hypotheses if allowed and h.mechanism not in allowed]
        hypotheses = [h for h in hypotheses if h not in dropped]
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
            outcome=_outcome(final, hypotheses),
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
                "outcome": finding.outcome,
                "dropped_out_of_role": [h.mechanism.value for h in dropped],
            },
            agent=role.value,
            usage=finding.usage,
        )
        return finding

    def _trace_model_calls(self, calls: Sequence[ModelCall], step: int, agent: str) -> None:
        """One `llm.call` event per HTTP request a model provider sent for this step."""
        for call in calls:
            self.tracer.emit(
                TraceCategory.LLM_CALL,
                {"step": step, **call.model_dump(mode="json")},
                agent=agent,
                duration_ms=call.duration_ms,
            )


def _outcome(final: InvestigationDecision | None, hypotheses: Sequence[Hypothesis]) -> str:
    """How an agent ended, for its finding (see AgentFinding.outcome)."""
    if final is None or final.action != "finish":
        return "step_limit"
    if final.kind in ("need_more_evidence", "stop", "no_usable_answer"):
        return final.kind
    return "hypothesis" if hypotheses else "need_more_evidence"
