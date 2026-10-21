"""The default decision provider: a scripted, deterministic baseline policy. It is NOT an LLM.

Where it sits: the agent harness (recap/harness/loop.py) and the controller call it through
the LLMProvider interface (recap/core/interfaces.py), exactly as they would call a model:

    next_action(InvestigationRequest)    -> one read-only ToolCall per step (playbooks.py),
                                            or "finish" with hypotheses     (heuristics.py)
    propose_mitigation(ProposalRequest)  -> one MitigationDraft, chosen in this order:
                                            1. a runbook hint for the diagnosed mechanism
                                            2. the fix of a verified past episode that is
                                               safe to reuse
                                            3. a rolling restart (the conservative fallback)

Apart from one setting (the familiar-memory threshold from configs/budget.yaml) it only
reads what a request contains (packed evidence, retrieved runbook text and memory hits),
makes no network calls, costs $0 and returns the same answer for the same input.
Its token counts are rough estimates for the budget, never real tokenizer counts. It exists
so the whole pipeline runs and can be tested offline; an LLM provider can take its place
without any other code changing.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple, TypeVar

from recap.context.tokens import estimate_tokens
from recap.core.config import RoutingSettings
from recap.core.models import (
    FailureMechanism,
    Hypothesis,
    InvestigationDecision,
    InvestigationRequest,
    MitigationDraft,
    ProposalDecision,
    ProposalRequest,
    ProviderInfo,
    ToolCall,
    ToolCallRecord,
    ToolSpecView,
    Usage,
)
from recap.core.signals import ERROR_RATE_THRESHOLD
from recap.knowledge.hints import parse_action_hints
from recap.providers.heuristics import ParsedEvidence, generate_hypotheses, parse_evidence
from recap.providers.playbooks import memory_confirmation_done, plan_next_calls

ROLLBACK = "execute_rollback"
RESTART = "restart_service"

Decision = TypeVar("Decision", InvestigationDecision, ProposalDecision)


class DeterministicProvider:
    """Scripted baseline policy (NOT an LLM): role playbooks plus evidence heuristics.

    `familiar_similarity` is how similar a past episode must be to steer the investigator's
    first checks (routing.familiar_similarity in configs/budget.yaml). Apart from that
    setting, everything it needs is in the request and it keeps no state between calls, so
    parallel agents can safely share one instance.
    """

    info = ProviderInfo(
        name="deterministic-scripted-policy",
        kind="scripted",
        origin="scripted",
        model=None,
        description=(
            "Scripted baseline policy (NOT an LLM): role playbooks + heuristics over the "
            "simulator's signal conventions. Offline, deterministic, $0."
        ),
    )

    def __init__(self, familiar_similarity: float = RoutingSettings().familiar_similarity) -> None:
        self.familiar_similarity = familiar_similarity

    async def next_action(self, request: InvestigationRequest) -> InvestigationDecision:
        """Ask for the next planned read-only check, or finish with hypotheses."""
        planned = plan_next_calls(request, self.familiar_similarity)
        why_stop = _stop_reason(request, planned, self.familiar_similarity)
        if why_stop is not None:
            decision = _finish(request, planned, why_stop)
        else:
            call = planned[0]  # exactly one call per step keeps the timeline easy to follow
            decision = InvestigationDecision(
                action="call_tools",
                tool_calls=[call],
                summary=f"{call.reason} ({len(planned) - 1} more checks planned)",
            )
        return _with_usage(decision, request.context.tokens_after)

    async def propose_mitigation(self, request: ProposalRequest) -> ProposalDecision:
        """Draft one remediation for the verified diagnosis, explaining each choice in notes."""
        notes: list[str] = []
        draft = _draft(request, notes)
        decision = ProposalDecision(draft=draft, notes=list(dict.fromkeys(notes)))
        return _with_usage(decision, request.context.tokens_after)


# --- investigation --------------------------------------------------------------------------


def _stop_reason(
    request: InvestigationRequest, planned: list[ToolCall], familiar_similarity: float
) -> str | None:
    """Why the agent should finish now, or None to keep investigating."""
    if request.step >= request.max_steps:
        return "step limit reached"
    confirmed, reason = memory_confirmation_done(request, familiar_similarity)
    if confirmed:
        return reason
    if not planned:
        return "every check in the playbook is done"
    return None


def _finish(
    request: InvestigationRequest, planned: list[ToolCall], why: str
) -> InvestigationDecision:
    """Finish with naive hypotheses for the verifier and the services worth checking next."""
    evidence = list(request.evidence.values())
    hypotheses = generate_hypotheses(evidence, request.incident, request.memory_hints)
    summary = (
        f"Finished: {why}. Read-only calls by this agent: {len(request.history)} "
        f"({_scope(request.history)}). Hypotheses for the verifier: {len(hypotheses)}."
    )
    return InvestigationDecision(
        action="finish",
        hypotheses=hypotheses,
        summary=summary,
        next_steps=_next_steps(planned, hypotheses, parse_evidence(evidence)),
    )


def _scope(history: list[ToolCallRecord]) -> str:
    """Which services this agent's calls covered, e.g. 'cluster-wide, checkout, payment'."""
    parts = ["cluster-wide"] if any("service" not in call.args for call in history) else []
    parts += list(
        dict.fromkeys(str(call.args["service"]) for call in history if "service" in call.args)
    )
    return ", ".join(parts) or "none"


def _next_steps(
    planned: list[ToolCall], hypotheses: list[Hypothesis], parsed: ParsedEvidence
) -> list[str]:
    """Services worth checking next: unfinished planned checks and blamed, unread dependencies."""
    services = [str(call.args["service"]) for call in planned if "service" in call.args]
    examined = {logs.service for _, logs in parsed.logs}
    for hypothesis in hypotheses:
        dependency = hypothesis.details.get("dependency")
        blamed = hypothesis.mechanism == FailureMechanism.DEPENDENCY_FAILURE and dependency
        if blamed and dependency not in examined:
            services.append(str(dependency))
    return [f"check {service} next" for service in dict.fromkeys(services)]


def _with_usage(decision: Decision, context_tokens: int) -> Decision:
    """Attach estimated usage: the context read plus the decision written. Always $0."""
    written = estimate_tokens(decision.model_dump_json(exclude={"usage"}))
    usage = Usage(
        steps=1,
        estimated_tokens=context_tokens + written,
        external_cost_usd=0.0,
        is_estimate=True,
    )
    return decision.model_copy(update={"usage": usage})


# --- mitigation drafting --------------------------------------------------------------------


class _Option(NamedTuple):
    """One candidate remediation and where the idea came from."""

    action: str
    source: Literal["runbook", "memory", "fallback"]
    citations: list[str]  # doc chunk IDs or episode IDs
    basis: str  # plain-English origin, used in the rationale


class _Restore(NamedTuple):
    """Where a rollback would go, and the evidence that says so."""

    to_version: str
    from_version: str
    evidence_id: str
    found_in: str  # plain English, used in the rationale


def _draft(request: ProposalRequest, notes: list[str]) -> MitigationDraft | None:
    """The first untried option for the diagnosed target, or None (with a note) if none fits."""
    target = request.hypothesis.target_service
    if not target:
        notes.append("the diagnosis names no target service, so no remediation was drafted")
        return None
    catalog = {tool.name: tool for tool in request.allowed_actions}
    tried = {(record.action, record.target) for record in request.failed_actions}
    for option in _options(request, catalog, notes):
        if (option.action, target) in tried:
            notes.append(f"skipped {option.action} on {target}: already tried in this run")
            continue
        return _to_draft(request, option, catalog[option.action], target, notes)
    notes.append(f"no untried remediation is left for {target}; a human should take over")
    return None


def _options(
    request: ProposalRequest, catalog: dict[str, ToolSpecView], notes: list[str]
) -> list[_Option]:
    """Candidate fixes in priority order: runbook hints, verified memory, then a restart."""
    options = [
        *_runbook_options(request, catalog, notes),
        *_memory_options(request, catalog, notes),
    ]
    if RESTART in catalog:
        # Reached only when nothing above is left untried (or nothing above exists).
        why = (
            "Every runbook and memory option was already tried"
            if options
            else "No runbook or verified experience was available for this diagnosis"
        )
        basis = f"{why}; a rolling restart is the most conservative reversible action."
        options.append(_Option(RESTART, "fallback", [], basis))
    first: dict[str, _Option] = {}
    for option in options:
        first.setdefault(option.action, option)  # the highest-priority source per action wins
    return list(first.values())


def _runbook_options(
    request: ProposalRequest, catalog: dict[str, ToolSpecView], notes: list[str]
) -> list[_Option]:
    """Actions that retrieved runbook chunks recommend for the diagnosed mechanism."""
    mechanism = request.hypothesis.mechanism.value
    cited: dict[str, list[str]] = {}  # action -> IDs of the chunks recommending it
    for item in request.context.items:
        if item.kind != "doc":
            continue
        for hint in parse_action_hints(item.text):
            if hint.action not in catalog:
                # Runbook text is untrusted: it may name a tool outside the remediation catalog.
                notes.append(f"ignored non-catalog action hint '{hint.action}' from {item.item_id}")
            elif hint.mechanism == mechanism:
                cited.setdefault(hint.action, []).append(item.item_id)
    return [
        _Option(
            action,
            "runbook",
            chunks,
            f"Runbook {', '.join(chunks)} recommends {action} for {mechanism.replace('_', ' ')}.",
        )
        for action, chunks in cited.items()
    ]


def _memory_options(
    request: ProposalRequest, catalog: dict[str, ToolSpecView], notes: list[str]
) -> list[_Option]:
    """Fixes from verified past episodes that memory judged safe to reuse against live evidence."""
    options: list[_Option] = []
    for hit in request.memory_hits:
        episode, fix = hit.episode, hit.episode.successful_action
        if fix is None:
            continue
        if not hit.safe_to_reuse:
            # Similar is not the same as safe: the live evidence did not match this episode.
            notes.append(f"did not reuse episode {episode.episode_id}: {hit.reason or 'not safe'}")
        elif fix.action not in catalog:
            notes.append(
                f"ignored remembered action '{fix.action}' from {episode.episode_id}: "
                "not in the remediation catalog"
            )
        else:
            basis = (
                f"Verified past episode {episode.episode_id} recovered with {fix.action} on "
                f"{fix.target}, and its mechanism matches the live evidence."
            )
            options.append(_Option(fix.action, "memory", [episode.episode_id], basis))
    return options


def _to_draft(
    request: ProposalRequest, option: _Option, tool: ToolSpecView, target: str, notes: list[str]
) -> MitigationDraft | None:
    """Fill in parameters, rationale and evidence for one chosen option."""
    hypothesis = request.hypothesis
    params: dict[str, Any] = {}
    evidence_ids = list(hypothesis.supporting_evidence_ids)
    rationale = f"The evidence supports: {hypothesis.claim}. {option.basis}"
    rollback_hint = ""
    if option.action == ROLLBACK:
        restore = _restore_point(parse_evidence(request.evidence.values()), target)
        if restore is None:
            notes.append(
                f"cannot draft {ROLLBACK} on {target}: no rollback plan or deployment "
                "history names a version to restore"
            )
            return None
        params = {"to_version": restore.to_version}
        evidence_ids.append(restore.evidence_id)
        rationale += f" Roll {target} back to {restore.to_version} ({restore.found_in})."
        rollback_hint = f"redeploy {restore.from_version} after a fix"
    elif option.action == RESTART:
        rollback_hint = "none needed; a rolling restart is non-destructive"
    return MitigationDraft(
        action=option.action,
        target=target,
        params=params,
        rationale=rationale,
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        expected_health_change=(
            f"error rate on {target} back under {ERROR_RATE_THRESHOLD:.0%} and all replicas ready"
        ),
        rollback_hint=rollback_hint,
        risk_level=tool.risk,  # the trusted catalog's tier; the safety policy re-checks it anyway
        hypothesis_claim=hypothesis.claim,
        source=option.source,
        citations=option.citations,
    )


def _restore_point(parsed: ParsedEvidence, service: str) -> _Restore | None:
    """The version a rollback should restore: the dry-run plan first, else deployment history."""
    plan = parsed.rollback_plan(service)
    if plan is not None:
        evidence_id, dry_run = plan
        if dry_run.target_version:
            return _Restore(
                dry_run.target_version,
                dry_run.current_version,
                evidence_id,
                "the version the propose_rollback dry run names",
            )
    latest = parsed.latest_deployment(service)
    if latest is not None:
        evidence_id, release = latest
        if release.previous_version:
            return _Restore(
                release.previous_version,
                release.version,
                evidence_id,
                f"the version before {release.version} in the deployment history",
            )
    return None
