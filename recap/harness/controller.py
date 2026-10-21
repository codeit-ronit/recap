"""Incident controller: the trusted owner of one incident's lifecycle.

It walks the lifecycle state machine

    CREATED -> RETRIEVING -> INVESTIGATING -> PROPOSING -> AWAITING_APPROVAL
            -> MITIGATING -> VERIFYING -> RESOLVED   (or FAILED / BLOCKED / BUDGET_EXHAUSTED)

and saves a checkpoint after every transition. It routes each investigation round
(single vs parallel agents), enforces bounded retries, and is the ONLY component that
can execute a mitigation: always through the safety gateway, and only after the policy
and, when required, an exact human approval allow it. Recovery is judged by an
independent oracle, and every finished run is consolidated into experience memory.

After a fix (the recovery lifecycle, audit finding 2.6):
  * a live target gets a bounded stabilization window, probed every few seconds, before
    the oracle's verdict counts; a simulated target is judged at once;
  * when the fix did not recover the system, a new "epoch" starts: agents observe again,
    and the verifier ignores state observations of the fixed service from before the fix;
  * every provider call is bounded by the run's own wall-clock budget, so a hanging model
    ends the run as BUDGET_EXHAUSTED at the deadline instead of running on.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from recap import __version__
from recap.agents.roles import agent_specs
from recap.context.evidence import EvidenceRegistry
from recap.context.packer import ContextPacker
from recap.core.config import EnvironmentConfig, Settings
from recap.core.errors import (
    ApprovalError,
    BudgetExhausted,
    ConfigError,
    QuotaExhausted,
    model_calls_of,
)
from recap.core.ids import new_id, utcnow
from recap.core.interfaces import (
    ApprovalChannel,
    LLMProvider,
    RecoveryOracle,
    RetrievalIndex,
    RouterPolicy,
    SafetyPolicy,
    TargetEnvironment,
    ToolTransport,
    TraceSink,
    Verifier,
)
from recap.core.lifecycle import Lifecycle
from recap.core.models import (
    SCHEMA_VERSION,
    ActionRecord,
    AgentFinding,
    Approval,
    EnvironmentInfo,
    Evidence,
    Hypothesis,
    Incident,
    IncidentState,
    MitigationProposal,
    OracleProbe,
    ProposalRequest,
    RetrievedChunk,
    RunBudget,
    RunConfig,
    RunRecord,
    ScenarioInfo,
    ToolRisk,
    Usage,
    Verdict,
    VerificationReport,
)
from recap.harness.budget import BudgetTracker
from recap.harness.loop import AgentHarness
from recap.harness.store import RunStore
from recap.knowledge.hints import parse_action_hints
from recap.memory.consolidation import Consolidator
from recap.memory.similarity import MemoryQuery
from recap.memory.store import ExperienceStore, MemoryAccess, assess_reuse
from recap.providers.prompts import PROMPT_VERSION
from recap.routing.orchestrator import Orchestrator
from recap.routing.signals import compute_signals
from recap.safety.approvals import ApprovalStore, AutoApprovalChannel, ExternalApprovalChannel
from recap.safety.capabilities import CapabilityIssuer
from recap.telemetry.tracer import TraceCategory, Tracer
from recap.telemetry.usage import cost_label
from recap.tools.gateway import ToolGateway
from recap.tools.registry import ToolRegistry
from recap.verification.oracle import EnvironmentHealthOracle, recovery_result

S = IncidentState
TransportFactory = Callable[[EnvironmentConfig, str], AbstractAsyncContextManager[ToolTransport]]
UNSAFE_RISKS = {ToolRisk.DESTRUCTIVE, ToolRisk.HIGH_RISK}
# Results of an action that actually reached the target (or tried to).
SENT_RESULTS = {"recovered", "not_recovered", "error"}
EXECUTED_RESULTS = {"recovered", "not_recovered"}
# Scenario files are data: their run_overrides may pick the red-team planner or a strategy,
# never switch off safety, approvals or memory isolation, or change the provider. (A
# scenario's separate `budget:` field only shapes that scenario's own RunBudget.)
SCENARIO_OVERRIDABLE = frozenset({"planner", "strategy"})
T = TypeVar("T")


@dataclass(frozen=True)
class ControllerDeps:
    """Long-lived components a controller needs. Built once by recap.runtime.Runtime."""

    settings: Settings
    registry: ToolRegistry
    policy: SafetyPolicy
    approvals: ApprovalStore
    knowledge: RetrievalIndex | None
    memory: ExperienceStore
    router: RouterPolicy
    verifier: Verifier
    consolidator: Consolidator
    packer: ContextPacker
    runs: RunStore
    trace_sinks: Callable[[str], list[TraceSink]]
    redactor: Callable[[Any], Any]
    provider_factory: Callable[[RunConfig], LLMProvider]
    environment_factory: Callable[[EnvironmentConfig, str], TargetEnvironment]
    transport_factory: TransportFactory
    fixture_hash: str = ""
    code_version: str = ""  # the git commit RECAP runs from ("+dirty" with uncommitted changes)
    # (environment, max_error_rate) -> oracle. A benchmark adapter can bring its own judge.
    oracle_factory: Callable[[TargetEnvironment, float], RecoveryOracle] = EnvironmentHealthOracle


class IncidentController:
    """Runs exactly one incident from alert to a terminal state."""

    def __init__(
        self,
        deps: ControllerDeps,
        *,
        scenario_id: str,
        config: RunConfig,
        approval_channel: ApprovalChannel | None = None,
        run_id: str | None = None,
    ) -> None:
        self.deps = deps
        self.settings = deps.settings
        self.scenario_id = scenario_id
        self.requested = config
        self.channel = approval_channel
        self.run_id = run_id or new_id("run")
        self.provider_usage = Usage()
        self.cancelled = False
        # Per-run state, created in run() / _build_run_components():
        self.environment: TargetEnvironment
        self.info: EnvironmentInfo
        self.scenario: ScenarioInfo
        self.config: RunConfig
        self.provider: LLMProvider
        self.incident: Incident
        self.budget: BudgetTracker
        self.lifecycle: Lifecycle
        self.record: RunRecord
        self.tracer: Tracer
        self.evidence: EvidenceRegistry
        self.oracle: RecoveryOracle
        self.gateway: ToolGateway
        self.capabilities: CapabilityIssuer
        self.harness: AgentHarness
        self.orchestrator: Orchestrator
        self.investigation_round = 0
        self.epoch_findings: list[AgentFinding] = []
        # service -> environment time of the last fix executed on it (for the verifier).
        self.remediated: dict[str, datetime] = {}
        # The fix being sent right now, so a cancellation can record what happened to it.
        self.mitigating: MitigationProposal | None = None

    # ==================================================================================
    # Entry point
    # ==================================================================================

    async def run(self) -> RunRecord:
        """Execute the whole lifecycle and return the final checkpoint."""
        env_config = self._environment_config()
        self.environment = self.deps.environment_factory(env_config, self.run_id)
        self.scenario = self._find_scenario()
        self.config = self._effective_config()
        self.info = self.environment.describe()  # is_simulated is fixed per adapter
        self._check_safety_scope()
        self.provider = self.deps.provider_factory(self.config)
        seed = self._seed()

        # Inject the scenario's fault (trusted) and turn its alert into a typed incident.
        alert = await self.environment.reset(self.scenario.id, seed)
        # Describe again: details such as the namespace belong to this incident's state.
        self.info = self.environment.describe()
        self.incident = Incident(
            incident_id=new_id("inc"),
            scenario_id=self.scenario.id,
            service=alert.service,
            affected_services=alert.affected_services,
            symptoms=alert.symptoms,
            created_at=alert.fired_at,
            environment=self.info.kind,
            metadata=alert.metadata,
            budget=self._budget(),
        )
        # This run's signing key: its private half stays in this process; the environment
        # learns only the public half and refuses any write not signed with it.
        self.capabilities = CapabilityIssuer(self.run_id, self.incident.incident_id)
        self.environment.bind_executor(self.capabilities.public_key)
        self.budget = BudgetTracker(self.incident.budget)
        self.lifecycle = Lifecycle()
        self.record = RunRecord(
            run_id=self.run_id,
            scenario_id=self.scenario.id,
            config=self.config,
            incident=self.incident,
            history=list(self.lifecycle.history),
            environment=self.info,
            provider=self.provider.info.name,
            provider_info=self.provider.info,
            versions=self._versions(seed),
        )
        self.tracer = Tracer(
            self.run_id, sinks=self.deps.trace_sinks(self.run_id), redact=self.deps.redactor
        )
        self.evidence = EvidenceRegistry(
            self.run_id,
            self.incident.incident_id,
            on_add=lambda ev: self.deps.runs.add_evidence(self.run_id, ev),
        )
        self.oracle = self.deps.oracle_factory(
            self.environment, self.settings.policies.oracle.max_error_rate
        )
        self.tracer.emit(
            TraceCategory.STATE,
            {
                "from": None,
                "to": S.CREATED.value,
                "reason": "incident created from alert",
                "incident": self.incident.model_dump(mode="json"),
                "environment": self.info.model_dump(mode="json"),
                "provider": self.provider.info.model_dump(mode="json"),
            },
        )
        self._checkpoint()

        try:
            if self._namespace_refused(alert.namespace):
                return self.record  # finally: consolidate + summarize
            async with self.deps.transport_factory(env_config, self.run_id) as transport:
                self._build_run_components(transport)
                await self.gateway.discover()
                await self._retrieve()
                await self._resolve()
        except asyncio.CancelledError:
            self.cancelled = True
            self._record_interrupted_fix("cancelled")
            self._finish(S.FAILED, "run cancelled")
            raise
        except Exception as exc:  # budget stops and any bug or provider error end the run cleanly
            self._stop_on_error(_unwrap_group(exc))
        finally:
            if self.lifecycle.is_terminal:
                self._consolidate()
                self._summarize()
        return self.record

    # ==================================================================================
    # Stages
    # ==================================================================================

    async def _retrieve(self) -> None:
        """RETRIEVING: fetch relevant runbooks (RAG) and similar past episodes (memory)."""
        self._transition(S.RETRIEVING, "gather runbooks and past experience")
        incident = self.incident
        knowledge = self.deps.knowledge
        if self.config.use_rag and knowledge is not None:
            query = " ".join([incident.service, *incident.symptoms])
            docs = knowledge.search(query, k=4)
            self.record.retrieved_docs = docs
            self.tracer.emit(
                TraceCategory.RETRIEVAL,
                {
                    "stage": "initial",
                    "reason": "look up runbooks relevant to the alert",
                    "query": query,
                    "retriever": knowledge.name,
                    "results": [_doc_ref(d) for d in docs],
                },
            )
        else:
            self.tracer.emit(
                TraceCategory.RETRIEVAL_SKIP,
                {"stage": "initial", "reason": "RAG disabled by run configuration"},
            )

        if self.config.use_memory:
            access = MemoryAccess.from_run_config(self.config)
            self.record.memory_hits = self.deps.memory.search(
                MemoryQuery.from_incident(incident), access
            )
            self.tracer.emit(
                TraceCategory.MEMORY_SEARCH,
                {
                    "readable_partitions": [p.value for p in access.readable_partitions],
                    "hits": [_hit_ref(h) for h in self.record.memory_hits],
                },
            )
        else:
            self.tracer.emit(
                TraceCategory.MEMORY_SKIP, {"reason": "memory disabled by run configuration"}
            )
        self.harness.docs = self.record.retrieved_docs
        self.harness.memory_hits = self.record.memory_hits
        self._checkpoint()

    async def _resolve(self) -> None:
        """Investigate -> propose -> approve -> mitigate -> verify, with bounded retries."""
        retries = self.settings.budget.retries
        failed_actions: list[ActionRecord] = []
        focus: list[str] = []
        attempts = 0
        while True:
            self._transition(S.INVESTIGATING, "investigate with read-only tools")
            report = await self._investigate(focus, len(failed_actions))
            if report is None or report.best is None:
                self._finish(
                    S.FAILED,
                    "evidence stayed insufficient after the allowed investigation rounds; "
                    "a human should take over",
                )
                return

            self._transition(S.PROPOSING, f"diagnosis: {report.best.claim}")
            proposal = await self._propose(report, failed_actions)
            if proposal is None:
                return  # _propose already moved the run to a terminal state

            approval = await self._authorize(proposal)
            if self.lifecycle.is_terminal:
                return

            self._transition(S.MITIGATING, f"{proposal.action} on {proposal.target}")
            before = await self.oracle.probe()
            # Until its outcome is recorded below, a cancellation records this fix instead
            # (see _record_interrupted_fix): an executed write must never vanish from the record.
            self.mitigating = proposal
            result = await self.gateway.execute_mitigation(proposal, approval)
            if approval is not None:  # show the store's copy: it records the single use
                stored = self.deps.approvals.get_approval(approval.approval_id)
                self.record.approvals[-1] = stored or approval
            if result.decision is not None and result.decision.verdict != Verdict.ALLOW:
                self.mitigating = None
                self.record.safety_decisions.append(result.decision)
                self._record_action(proposal, "denied", "; ".join(result.decision.reasons))
                self._finish(S.BLOCKED, "the gateway refused to execute the mitigation")
                return
            attempts += 1
            if result.ok:  # the target changed: its older state observations are now stale
                # The boundary is the oracle's own "before" reading (environment time from
                # the trusted channel), never a time the tool reported about itself.
                for service in self._fixed_services(proposal):
                    self.remediated[service] = before.checked_at

            self._transition(S.VERIFYING, "independent oracle checks recovery")
            # A write the target explicitly refused changed nothing: no reason to wait for it
            # to settle. A transport error or timeout leaves the outcome unknown, so wait.
            refused = not result.ok and result.output is not None
            after, probes, waited = await self._await_recovery(settle=not refused)
            recovery = recovery_result(before, after, self.oracle.name)
            self.record.recovery = recovery
            self.tracer.emit(
                TraceCategory.VERIFY_RECOVERY,
                {
                    "oracle": recovery.oracle_name,
                    "healthy_before": recovery.healthy_before,
                    "healthy_after": recovery.healthy_after,
                    "verified": recovery.verified,
                    "unhealthy_after": after.unhealthy_services,
                    "simulated": self.info.is_simulated,
                    "probes": probes,
                    "waited_seconds": waited,
                },
            )
            outcome = "recovered" if recovery.verified else "not_recovered"
            note = "" if result.ok else f"tool error: {result.error}"
            self.mitigating = None
            action = self._record_action(proposal, outcome if result.ok else "error", note)
            if recovery.verified:
                where = "simulated environment" if self.info.is_simulated else self.info.kind
                self._finish(S.RESOLVED, f"oracle verified recovery ({where})")
                return

            failed_actions.append(action)
            if attempts >= retries.max_mitigation_attempts:
                self._finish(S.FAILED, "mitigation did not recover the system; attempts used up")
                return
            # Start a new epoch: the system changed, so agents may observe it again. The
            # oracle's verdict is used only as a yes/no; where to look next comes from
            # RECAP's own reasoning, never from the oracle's list of unhealthy services.
            focus = _retry_focus(self.deps.registry, proposal, report)
            self.harness.epoch_start = len(self.evidence)
            self.epoch_findings = []

    async def _investigate(
        self, focus: list[str], failed_attempts: int
    ) -> VerificationReport | None:
        """INVESTIGATING: route, run agents, verify; escalate when evidence is insufficient."""
        rounds = self.settings.budget.retries.max_investigation_rounds
        report: VerificationReport | None = None
        escalate = failed_attempts > 0
        for _ in range(rounds):
            self.investigation_round += 1
            signals = compute_signals(
                incident=self.incident,
                memory_hits=self.record.memory_hits,
                budget=self.budget,
                routing=self.settings.budget.routing,
                prior_failed_attempts=failed_attempts,
                escalation=escalate,
            )
            decision = self.deps.router.decide(signals, self.config, self.investigation_round)
            decision = decision.model_copy(update={"provider": self.provider.info.name})
            self.record.strategies.append(decision)
            self.tracer.emit(
                TraceCategory.ROUTING,
                {
                    "round": self.investigation_round,
                    "mode": decision.mode.value,
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                    "agents": [a.value for a in decision.selected_agents],
                    "budget": decision.budget,
                    "signals": signals.model_dump(mode="json"),
                    "router": self.deps.router.name,
                },
            )
            self.tracer.emit(
                TraceCategory.INVESTIGATION_ROUND,
                {
                    "round": self.investigation_round,
                    "focus_services": focus,
                    "escalation": escalate,
                },
            )
            findings = await self.orchestrator.execute(decision, focus)
            self._add_findings(findings)
            if findings and all(f.error for f in findings):
                # Every agent crashed (e.g. the provider is failing): more rounds would only
                # repeat the failure, and "insufficient evidence" would hide the real cause.
                self._fail_with_error(
                    "; ".join(f"{f.agent_role.value}: {f.error}" for f in findings),
                    "every agent in the round failed; see run.error",
                )
                return None

            report = self.deps.verifier.verify(
                self.epoch_findings, self.evidence, self.incident, remediated=self.remediated
            )
            self.record.verification = report
            self.tracer.emit(TraceCategory.VERIFY_EVIDENCE, _report_payload(report))
            self._assess_memory(report)
            # Later rounds must see the evidence that contradicts a hypothesis.
            self.harness.priority_ids = _contradicting_ids(report)
            self._checkpoint()
            if report.status == "supported" and report.best is not None:
                return report
            escalate = True
            focus = report.focus_services or focus
            if self.budget.remaining_tool_calls() == 0:
                break
        return report

    async def _propose(
        self, report: VerificationReport, failed_actions: list[ActionRecord]
    ) -> MitigationProposal | None:
        """PROPOSING: draft a fix, then pass it through the policy and the evidence check."""
        best = report.best
        assert best is not None
        self._maybe_retrieve_runbook(best)

        target = best.target_service
        priority = [*best.supporting_evidence_ids, *_contradicting_ids(report)]
        # The runbook chunks that recommend a fix for the diagnosed mechanism are pinned:
        # a crowded pack must never drop them, or the fix falls back to a blind restart.
        pinned = _runbooks_for(best, self.record.retrieved_docs)
        # Providers get label-free views: no scenario ID, no evaluation labels.
        incident = self.incident.agent_view()
        memories = [hit.agent_view() for hit in self.record.memory_hits]
        pack = self.deps.packer.build(
            incident=incident,
            evidence=self.evidence.all(),
            docs=self.record.retrieved_docs,
            memories=memories,
            max_tokens=self.settings.budget.context.pack_tokens,
            focus_services=[target] if target else [],
            priority_ids=priority,
            pinned_ids=pinned,
        )
        self.budget.charge_step()
        self.budget.charge_tokens(pack.tokens_after)
        self.tracer.emit(
            TraceCategory.CONTEXT,
            {
                "stage": "proposal",
                "tokens_before": pack.tokens_before,
                "tokens_after": pack.tokens_after,
                "budget_tokens": pack.budget_tokens,
                "items": pack.ids(),
                "dropped": pack.dropped_ids,
                "priority": priority,
                "pinned": pinned,
                "estimate": True,
            },
        )
        request = ProposalRequest(
            incident=incident,
            hypothesis=best,
            report=report,
            context=pack,
            evidence=self._proposal_evidence(pack.ids("evidence"), best),
            allowed_actions=self.deps.registry.remediation_views(),
            memory_hits=memories,
            failed_actions=failed_actions,
        )
        try:
            decision = await self._before_deadline(
                self.provider.propose_mitigation(request), "the remediation proposal"
            )
        except Exception as exc:
            # Requests a real model already answered before the proposal failed still count.
            calls, spent = model_calls_of(exc)
            self._trace_model_calls(calls)
            if spent is not None:
                self._add_usage(spent)
            raise
        self._add_usage(decision.usage)
        self._trace_model_calls(decision.model_calls)  # a real model's requests behind it
        if decision.draft is None:
            self.tracer.emit(TraceCategory.PROPOSAL, {"proposal": None, "notes": decision.notes})
            self._finish(S.FAILED, "no remediation could be drafted; a human should take over")
            return None

        proposal = MitigationProposal(
            **decision.draft.model_dump(),
            proposal_id=new_id("prop"),
            incident_id=self.incident.incident_id,
        )
        self.record.proposals.append(proposal)
        self.tracer.emit(
            TraceCategory.PROPOSAL,
            {
                "proposal": proposal.model_dump(mode="json"),
                "digest": proposal.digest(),
                "notes": decision.notes,
                "trusted_risk": _risk(self.deps.registry, proposal.action),
            },
        )

        # Hard gate first: trusted policy code decides, whatever the proposal claims.
        safety = self.gateway.evaluate_mitigation(proposal)
        self.record.safety_decisions.append(safety)
        if safety.verdict == Verdict.DENY:
            self._record_action(proposal, "denied", "; ".join(safety.reasons))
            self._finish(S.BLOCKED, f"safety policy denied {proposal.action}")
            return None

        # Evidence gate: the proposal must cite evidence from this run and match the diagnosis.
        # Like the policy, it is an execution guardrail, so the simulation-only "safety off"
        # ablation records what it would have said but does not let it block.
        check = self.deps.verifier.check_proposal(proposal, report, self.evidence)
        enforced = self.config.safety_enabled
        self.tracer.emit(
            TraceCategory.VERIFY_EVIDENCE,
            {
                "proposal_check": check.model_dump(mode="json"),
                "proposal_id": proposal.proposal_id,
                "enforced": enforced,
            },
        )
        if not check.ok and enforced:
            self._record_action(proposal, "denied", "; ".join(check.problems))
            self._finish(S.BLOCKED, "proposal is not supported by recorded evidence")
            return None
        self._checkpoint()
        return proposal

    async def _authorize(self, proposal: MitigationProposal) -> Approval | None:
        """AWAITING_APPROVAL: get an exact, single-use approval when the policy requires one."""
        decision = self.record.safety_decisions[-1]
        if decision.verdict == Verdict.ALLOW:
            # Only possible when safety is disabled in a simulation-only ablation.
            return None
        self._transition(S.AWAITING_APPROVAL, f"approval required for {proposal.action}")
        ttl = self.settings.budget.approvals.ttl_seconds
        request = self.deps.approvals.create_request(proposal, self.run_id, ttl_seconds=ttl)
        channel = self._approval_channel()
        self.record.pending_approval = request
        self.tracer.emit(
            TraceCategory.APPROVAL_REQUESTED,
            {
                "proposal_id": proposal.proposal_id,
                "digest": request.proposal_digest,
                "expires_at": request.expires_at.isoformat(),
                "channel": channel.name,
            },
        )
        self._checkpoint()

        approval: Approval | None = None
        self.budget.pause_clock()  # waiting for a human does not consume the run budget
        try:
            try:
                await channel.notify(request)
            except ApprovalError as exc:  # e.g. a console answer that came after the expiry
                self.tracer.emit(
                    TraceCategory.APPROVAL_DECIDED, {"decision": "refused", "reason": str(exc)}
                )
            approval = await self.deps.approvals.wait_for_decision(proposal.proposal_id, ttl)
        finally:
            self.budget.resume_clock()
            self.record.pending_approval = None
            if approval is None:
                # Expired or cancelled: make sure nobody can approve this request later.
                self.deps.approvals.expire(proposal.proposal_id)

        if approval is None:
            self.tracer.emit(TraceCategory.APPROVAL_DECIDED, {"decision": "expired"})
            self._record_action(proposal, "expired", "approval expired")
            self._finish(S.BLOCKED, "approval request expired")
            return None
        self.record.approvals.append(approval)
        # How the decision was made, from the append-only decision log (the console records
        # "interactive terminal; answered after 53.4 s"; other channels leave it empty).
        logged = [
            event.detail
            for event in self.deps.approvals.events(proposal.proposal_id)
            if event.kind == approval.decision
        ]
        self.tracer.emit(
            TraceCategory.APPROVAL_DECIDED,
            {
                "decision": approval.decision,
                "actor": approval.actor,
                "approval_id": approval.approval_id,
                "proposal_id": approval.proposal_id,
                "note": logged[-1] if logged else "",
            },
        )
        if approval.decision != "approved":
            self._record_action(proposal, "rejected", f"rejected by {approval.actor}")
            self._finish(S.BLOCKED, f"mitigation rejected by {approval.actor}")
            return None
        return approval

    async def _await_recovery(self, settle: bool = True) -> tuple[OracleProbe, int, float]:
        """VERIFYING: probe the oracle until the system is healthy or had time to settle.

        A simulated target recovers the moment a fix is applied (its clock jumps forward), so
        it is probed once; so is a target that refused the write (`settle=False`). A live
        target needs time: pods restart and readiness probes must pass. It is probed every
        `stabilization_poll_seconds` until it is healthy or its `stabilization_timeout_seconds`
        are over. If the run's own time runs out first, the verdict is not reached: that is a
        budget stop (BUDGET_EXHAUSTED), not evidence that the fix failed.
        Returns the last probe, the number of probes and the seconds spent waiting.
        """
        started = time.monotonic()
        after = await self.oracle.probe()
        probes = 1
        window = (
            0.0 if self.info.is_simulated or not settle else self.info.stabilization_timeout_seconds
        )
        while not after.healthy:
            window_left = window - (time.monotonic() - started)
            if window_left <= 0:
                break  # the target had its full window: the verdict stands
            run_left = self.budget.remaining_seconds()
            if run_left <= 0:
                detail = "the run's time ran out while waiting for the target to settle"
                raise BudgetExhausted("max_elapsed_seconds", detail)
            await asyncio.sleep(min(self.info.stabilization_poll_seconds, window_left, run_left))
            after = await self.oracle.probe()
            probes += 1
        return after, probes, round(time.monotonic() - started, 3)

    async def _before_deadline(self, call: Awaitable[T], what: str) -> T:
        """Await a provider call, but never past the run's wall-clock budget.

        A model that hangs would otherwise keep the run going long after its deadline.
        """
        try:
            async with asyncio.timeout(self.budget.remaining_seconds()) as deadline:
                return await call
        except TimeoutError:
            if not deadline.expired():
                raise  # the call itself raised TimeoutError: an error, not the run's deadline
            detail = f"{what} was still running when the run's time ran out"
            raise BudgetExhausted("max_elapsed_seconds", detail) from None

    # ==================================================================================
    # Helpers
    # ==================================================================================

    def _record_interrupted_fix(self, how: str) -> None:
        """The run stopped between sending a fix and recording it: record what happened.

        `how` is "cancelled" or "stopped" (a budget stop or an error). A write that reached the
        target cannot be called back, so the gateway waits for its outcome even when the run is
        cancelled mid-send; a stop can also arrive while the controller waits for a live target
        to settle. Either way the record must show the fix, whether the tool reported it done,
        and the stored (used) approval. Its recovery was not verified.
        """
        proposal = self.mitigating
        if proposal is None:
            return
        self.mitigating = None
        if self.record.approvals:  # the store's copy records the single use
            latest = self.record.approvals[-1]
            stored = self.deps.approvals.get_approval(latest.approval_id)
            self.record.approvals[-1] = stored or latest
        write = self.gateway.last_write  # reset by the gateway at the start of every write
        if write is None:
            sent = "it was not sent"
        elif write.ok:
            sent = "the tool reported it done"
        else:
            sent = f"it failed: {write.error}"
        when = "being sent" if self.lifecycle.state == S.MITIGATING else "awaiting verification"
        note = f"the run was {how} while this fix was {when}; {sent}; recovery not verified"
        self._record_action(proposal, "error", note)

    def _fixed_services(self, proposal: MitigationProposal) -> list[str]:
        """The services a fix changed: its service target, or every service for a namespace-wide
        action (possible only in the simulation-only safety-off ablation)."""
        if self.deps.registry.target_arg(proposal.action) == "service":
            return [proposal.target]
        return list(self.settings.policies.allowed_services)

    def _namespace_refused(self, alert_namespace: str) -> bool:
        """Refuse up front to work in a namespace the safety policy does not allow."""
        allowed = self.settings.policies.allowed_namespaces
        namespace = self.info.namespace
        if namespace in allowed and alert_namespace == namespace:
            return False
        self._finish(
            S.BLOCKED,
            f"namespace '{alert_namespace}' is not in the safety policy's allowlist "
            f"{allowed}; nothing was run",
        )
        return True

    def _build_run_components(self, transport: ToolTransport) -> None:
        """Create the per-run gateway, harness and orchestrator around one MCP connection."""
        self.gateway = ToolGateway(
            transport=transport,
            registry=self.deps.registry,
            policy=self.deps.policy,
            approvals=self.deps.approvals,
            budget=self.budget,
            evidence=self.evidence,
            tracer=self.tracer,
            incident=self.incident,
            environment=self.info,
            limits=self.settings.policies.limits,
            target_scope=self._target_scope(),
            safety_enabled=self.config.safety_enabled,
            redact=self.deps.redactor,
            capabilities=self.capabilities,
        )
        self.record.versions["transport"] = getattr(transport, "transport_name", "unknown")
        self.harness = AgentHarness(
            incident=self.incident,
            provider=self.provider,
            gateway=self.gateway,
            packer=self.deps.packer,
            evidence=self.evidence,
            tracer=self.tracer,
            budget=self.budget,
            pack_tokens=self.settings.budget.context.pack_tokens,
        )
        self.harness.on_usage = self._add_usage
        self.orchestrator = Orchestrator(
            self.harness.run_agent,
            agent_specs(self.settings),
            self.tracer,
            agent_timeout_seconds=self.incident.budget.max_elapsed_seconds,
            remaining_seconds=self.budget.remaining_seconds,  # no agent outlives the run
        )

    def _trace_model_calls(self, calls: list[Any]) -> None:
        """One `llm.call` event per HTTP request a model provider sent for the proposal."""
        for call in calls:
            self.tracer.emit(
                TraceCategory.LLM_CALL, call.model_dump(mode="json"), duration_ms=call.duration_ms
            )

    def _target_scope(self) -> str:
        """Lock scope for conflicting-remediation checks: one sandbox per simulated run."""
        instance = self.run_id if self.info.is_simulated else self.info.name
        return f"{self.info.kind}:{instance}"

    def _add_findings(self, findings: list[AgentFinding]) -> None:
        self.record.findings.extend(findings)
        self.epoch_findings.extend(findings)

    def _add_usage(self, usage: Usage) -> None:
        """Count provider usage the moment it is produced (it survives crashes and stops)."""
        self.provider_usage = self.provider_usage.plus(usage)

    def _assess_memory(self, report: VerificationReport) -> None:
        """Re-label memory hits once live evidence is verified: similar is not safe-to-reuse."""
        if not self.record.memory_hits:
            return
        self.record.memory_hits = [assess_reuse(h, report) for h in self.record.memory_hits]
        self.harness.memory_hits = self.record.memory_hits
        self.tracer.emit(
            TraceCategory.MEMORY_ASSESS,
            {
                "hits": [
                    {
                        "episode_id": h.episode.episode_id,
                        "similarity": h.similarity,
                        "mechanism_match": h.mechanism_match,
                        "safe_to_reuse": h.safe_to_reuse,
                        "reason": h.reason,
                    }
                    for h in self.record.memory_hits
                ]
            },
        )

    def _maybe_retrieve_runbook(self, best: Hypothesis) -> None:
        """Agentic RAG: retrieve a remediation runbook only if none for this mechanism is loaded."""
        knowledge = self.deps.knowledge
        if not self.config.use_rag or knowledge is None:
            self.tracer.emit(
                TraceCategory.RETRIEVAL_SKIP,
                {"stage": "proposal", "reason": "RAG disabled by run configuration"},
            )
            return
        mechanism = best.mechanism.value
        loaded = any(
            hint.mechanism == mechanism
            for doc in self.record.retrieved_docs
            for hint in parse_action_hints(doc.chunk.text)
        )
        if loaded:
            self.tracer.emit(
                TraceCategory.RETRIEVAL_SKIP,
                {"stage": "proposal", "reason": f"a runbook for {mechanism} is already in context"},
            )
            return
        query = f"{mechanism.replace('_', ' ')} remediation recommended action runbook"
        known = {d.chunk.chunk_id for d in self.record.retrieved_docs}
        new_docs = [d for d in knowledge.search(query, k=2) if d.chunk.chunk_id not in known]
        self.record.retrieved_docs = [*self.record.retrieved_docs, *new_docs]
        self.harness.docs = self.record.retrieved_docs
        self.tracer.emit(
            TraceCategory.RETRIEVAL,
            {
                "stage": "proposal",
                "reason": f"no runbook for {mechanism} in context yet",
                "query": query,
                "retriever": knowledge.name,
                "results": [_doc_ref(d) for d in new_docs],
            },
        )

    def _proposal_evidence(self, packed_ids: list[str], best: Hypothesis) -> dict[str, Evidence]:
        """Packed evidence plus the diagnosis' cited evidence and the target's rollback plans."""
        ids = list(dict.fromkeys([*packed_ids, *best.supporting_evidence_ids]))
        plans = [
            e.id
            for e in self.evidence.all()
            if e.tool_name == "propose_rollback" and e.data.get("service") == best.target_service
        ]
        return {e.id: e for e in self.evidence.by_ids([*ids, *plans])}

    def _approval_channel(self) -> ApprovalChannel:
        if self.config.approval_mode == "auto":
            return AutoApprovalChannel(self.deps.approvals, self.info)
        return self.channel or ExternalApprovalChannel()

    def _record_action(self, proposal: MitigationProposal, result: str, note: str) -> ActionRecord:
        action = ActionRecord(
            action=proposal.action,
            target=proposal.target,
            params=proposal.params,
            result=result,  # type: ignore[arg-type]
            note=note,
        )
        self.record.actions.append(action)
        return action

    def _transition(self, state: IncidentState, reason: str = "") -> None:
        previous = self.lifecycle.state
        change = self.lifecycle.transition(state, reason)
        self.record.state = state
        self.record.history.append(change)
        self.tracer.stage = state.value
        self.tracer.emit(
            TraceCategory.STATE, {"from": previous.value, "to": state.value, "reason": reason}
        )
        self._checkpoint()

    def _stop_on_error(self, exc: BaseException) -> None:
        """QuotaExhausted -> QUOTA_EXHAUSTED (deferred); BudgetExhausted -> BUDGET_EXHAUSTED
        (a clean, expected stop); anything else -> FAILED."""
        self._record_interrupted_fix("stopped")
        if isinstance(exc, QuotaExhausted):
            # A free model's quota ran out: nothing about the incident was decided, and RECAP
            # does not switch to another provider to finish the run.
            self.tracer.emit(TraceCategory.BUDGET, {"limit": exc.limit, "detail": exc.detail})
            self._finish(S.QUOTA_EXHAUSTED, f"deferred: {exc}")
            return
        if isinstance(exc, BudgetExhausted):
            self.tracer.emit(TraceCategory.BUDGET, {"limit": exc.limit, "detail": exc.detail})
            self._finish(S.BUDGET_EXHAUSTED, f"stopped safely: {exc}")
            return
        self._fail_with_error(f"{type(exc).__name__}: {exc}", "unexpected error; see run.error")

    def _fail_with_error(self, error: str, note: str) -> None:
        """End the run FAILED with a (redacted) error message in the record and the trace."""
        self.record.error = self.deps.redactor(error)
        self.tracer.emit(TraceCategory.RUN_ERROR, {"error": self.record.error})
        self._finish(S.FAILED, note)

    def _finish(self, state: IncidentState, note: str) -> None:
        """Move to a terminal state (once) and record why."""
        if self.lifecycle.is_terminal:
            return
        self.record.outcome_note = note
        self._transition(state, note)

    def _checkpoint(self) -> None:
        self.record.updated_at = utcnow()
        self.record.usage = self._usage()
        # A model provider learns its server's facts (version, weights) at its first request.
        self.record.provider_info = self.provider.info
        self.deps.runs.save(self.record)

    def _usage(self) -> Usage:
        """Tool calls, steps and tokens come from the budget tracker (the authoritative count)."""
        p = self.provider_usage
        return Usage(
            steps=self.budget.steps,
            tool_calls=self.budget.tool_calls,
            estimated_tokens=self.budget.tokens,
            actual_input_tokens=p.actual_input_tokens,
            actual_output_tokens=p.actual_output_tokens,
            external_cost_usd=p.external_cost_usd,
            is_estimate=p.is_estimate,
            unreported_calls=p.unreported_calls,
            model_requests=p.model_requests,
        )

    def _consolidate(self) -> None:
        """Write the finished run to experience memory (success only if the oracle verified it)."""
        if self.record.state == S.QUOTA_EXHAUSTED:
            # Deferred: the model never finished, so nothing about the incident was learned.
            reason = "run deferred (a free model's quota ran out); it is not experience"
            self.tracer.emit(TraceCategory.MEMORY_SKIP, {"reason": reason})
            return
        if self.cancelled or self.record.error:
            # A run that crashed or was cancelled says nothing about the incident itself.
            self.tracer.emit(
                TraceCategory.MEMORY_SKIP,
                {"reason": "run ended by an error or cancellation; it is not experience"},
            )
            return
        access = MemoryAccess.from_run_config(self.config)
        if not access.write_enabled:
            self.tracer.emit(
                TraceCategory.MEMORY_SKIP,
                {"reason": "memory writes disabled for this run (e.g. evaluation test scoring)"},
            )
            return
        try:
            episode = self.deps.consolidator.build_episode(
                record=self.record,
                evidence=self.evidence.all(),
                scenario=self.scenario,
                partition=self.config.memory_write_partition,
                simulated=self.info.is_simulated,
                environment_kind=self.info.kind,
            )
            stored = self.deps.memory.add(episode, access)
        except ValueError as exc:
            self.tracer.emit(TraceCategory.MEMORY_SKIP, {"reason": str(exc)})
            return
        if stored:
            self.record.episode_id = episode.episode_id
            self.tracer.emit(
                TraceCategory.MEMORY_WRITE,
                {
                    "episode_id": episode.episode_id,
                    "outcome": episode.outcome.value,
                    "verified": episode.verified,
                    "partition": episode.partition.value,
                    "mechanism": episode.failure_mechanism,
                },
            )
        self._checkpoint()

    def _summarize(self) -> None:
        """Emit the run summary; the eval report counts attempts and unsafe actions from it."""
        registry = self.deps.registry
        actions = self.record.actions
        unsafe = [p for p in self.record.proposals if _is_unsafe(registry, p.action)]
        executed = [
            a for a in actions if _is_unsafe(registry, a.action) and a.result in EXECUTED_RESULTS
        ]
        refused = [a for a in actions if _is_unsafe(registry, a.action) and a.result == "error"]
        usage = self._usage()
        self.tracer.emit(
            TraceCategory.RUN_SUMMARY,
            {
                "state": self.record.state.value,
                "outcome_note": self.record.outcome_note,
                "recovered": bool(self.record.recovery and self.record.recovery.verified),
                # Only actions that were sent to the target count as attempts.
                "mitigation_attempts": len([a for a in actions if a.result in SENT_RESULTS]),
                "unsafe_attempts": len(unsafe),
                "unsafe_blocked": len(unsafe) - len(executed) - len(refused),
                "unsafe_executed": len(executed),
                "unsafe_refused_by_environment": len(refused),
                "budget": self.budget.snapshot(),
                "cost": cost_label(
                    usage,
                    self.provider.info.origin,
                    # Set only by a free hosted provider whose tripwire fired (quota.py).
                    free_status_lost=getattr(self.provider, "free_status_lost", None),
                ),
                "simulated": self.info.is_simulated,
                "episode_id": self.record.episode_id,
            },
            usage=usage,
        )
        self._checkpoint()

    # --- configuration ----------------------------------------------------------------------

    def _environment_config(self) -> EnvironmentConfig:
        env = self.settings.environment
        if self.requested.tool_latency_ms is None:
            return env
        options = dict(env.options, tool_latency_ms=self.requested.tool_latency_ms)
        return env.model_copy(update={"options": options})

    def _find_scenario(self) -> ScenarioInfo:
        for scenario in self.environment.list_scenarios():
            if scenario.id == self.scenario_id:
                return scenario
        raise ConfigError(f"unknown scenario '{self.scenario_id}'")

    def _effective_config(self) -> RunConfig:
        """Scenario overrides (planner, strategy only) apply unless the caller set the field."""
        overrides = {
            key: value
            for key, value in self.scenario.run_overrides.items()
            if key in SCENARIO_OVERRIDABLE and key not in self.requested.model_fields_set
        }
        if not overrides:
            return self.requested
        return RunConfig.model_validate({**self.requested.model_dump(), **overrides})

    def _check_safety_scope(self) -> None:
        if not self.config.safety_enabled and not self.info.is_simulated:
            raise ConfigError("safety can only be disabled against a simulated environment")
        if self.config.approval_mode == "auto" and not self.info.is_simulated:
            raise ConfigError("the auto-approver is allowed only in a simulated environment")

    def _seed(self) -> int:
        if self.config.seed is not None:
            return self.config.seed
        return self.scenario.default_seed or 0

    def _budget(self) -> RunBudget:
        """The run's limits: defaults, then the provider's overrides, then the scenario's.

        A real model needs more time than the scripted policy (about a minute per decision
        locally, or the free plan's rate limits), so its entry in configs/providers.yaml may
        raise the limits. A scenario's own budget (e.g. budget-exhausted's 3 tool calls)
        still wins, and an explicit RunConfig.budget replaces everything.
        """
        if self.config.budget is not None:
            return self.config.budget
        defaults = self.settings.budget.default_budget.model_dump()
        # The provider's own overrides: a replay carries those of the RECORDED run, so it
        # gets the same limits (and the same "step N of M" in every prompt) as the original.
        provider_budget = getattr(self.provider, "budget_overrides", None)
        if provider_budget is None:
            name = self.config.provider or self.settings.providers.default
            spec = self.settings.providers.providers.get(name)
            provider_budget = spec.budget if spec is not None else {}
        return RunBudget(**{**defaults, **provider_budget, **self.scenario.budget})

    def _versions(self, seed: int) -> dict[str, Any]:
        return {
            "recap": __version__,
            "schema": SCHEMA_VERSION,
            # prompts-v4 for the free/local LLMs; otherwise prompts-v3, as recorded before
            # (the scripted policy itself uses no prompts at all).
            "prompt_version": self.provider.info.prompt_version or PROMPT_VERSION,
            "config_hash": self.settings.config_hash,
            "fixture_hash": self.deps.fixture_hash,
            "code_version": self.deps.code_version,
            "policy_version": self.settings.policies.version,
            "router": self.deps.router.name,
            "verifier": self.deps.verifier.name,
            "provider": self.provider.info.name,
            "provider_origin": self.provider.info.origin,
            "recording": self.provider.info.recording,
            "environment": self.info.kind,
            "seed": seed,
        }


# --- small pure helpers -----------------------------------------------------------------------


def _unwrap_group(exc: BaseException) -> BaseException:
    """Async libraries may wrap one error in an ExceptionGroup; look inside single-error groups."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _retry_focus(
    registry: ToolRegistry, proposal: MitigationProposal, report: VerificationReport
) -> list[str]:
    """Where the next round should look first after a fix that did not recover the system.

    Only what RECAP itself knows counts: the service the failed fix was aimed at (did the
    fix take effect?) and the services the evidence verifier asked about. The recovery
    oracle reads the environment's private ground truth; its list of unhealthy services
    is something no agent observed, so it must never become a hint (audit finding 2.3).
    """
    # Only service targets: a namespace-wide action has no service to look at again.
    services = [proposal.target] if registry.target_arg(proposal.action) == "service" else []
    return list(dict.fromkeys([*services, *report.focus_services]))


def _runbooks_for(best: Hypothesis, docs: list[RetrievedChunk]) -> list[str]:
    """IDs of retrieved runbook chunks that recommend an action for the diagnosed mechanism."""
    mechanism = best.mechanism.value
    return [
        doc.chunk.chunk_id
        for doc in docs
        if any(hint.mechanism == mechanism for hint in parse_action_hints(doc.chunk.text))
    ]


def _contradicting_ids(report: VerificationReport) -> list[str]:
    """Evidence IDs that contradict any checked hypothesis (kept visible in later packs)."""
    ids: list[str] = []
    for check in report.checks:
        ids += [i for i in check.hypothesis.contradicting_evidence_ids if i not in ids]
    return ids


def _doc_ref(doc: RetrievedChunk) -> dict[str, Any]:
    return {
        "chunk_id": doc.chunk.chunk_id,
        "doc_path": doc.chunk.doc_path,
        "heading": doc.chunk.heading,
        "score": round(doc.score, 3),
        "rank": doc.rank,
    }


def _hit_ref(hit: Any) -> dict[str, Any]:
    episode = hit.episode
    return {
        "episode_id": episode.episode_id,
        "similarity": hit.similarity,
        "outcome": hit.outcome.value,
        "verified": episode.verified,
        "mechanism": episode.failure_mechanism,
        "service": episode.service,
    }


def _report_payload(report: VerificationReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "summary": report.summary,
        "best": report.best.model_dump(mode="json") if report.best else None,
        "checks": [
            {
                "claim": c.hypothesis.claim,
                "mechanism": c.hypothesis.mechanism.value,
                "target": c.hypothesis.target_service,
                "origin": c.hypothesis.origin,
                "status": c.hypothesis.status.value,
                "passed": c.passed,
                "failed": c.failed,
                "rule_ids": c.rule_ids,
            }
            for c in report.checks
        ],
        "unknown_evidence_ids": report.unknown_evidence_ids,
        "focus_services": report.focus_services,
    }


def _risk(registry: ToolRegistry, tool: str) -> str:
    risk = registry.risk_of(tool)
    return risk.value if risk else "unknown"


def _is_unsafe(registry: ToolRegistry, tool: str) -> bool:
    """Destructive, high-risk or unknown tools count as unsafe attempts."""
    risk = registry.risk_of(tool)
    return risk is None or risk in UNSAFE_RISKS
