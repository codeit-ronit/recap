"""Evidence merger: pools the agents' claims and records where they disagree.

Where it sits (the evidence verifier, recap/agents/verifier.py, calls it):

    every agent's findings this epoch --> [merge_claims] --> one claim per distinct claim
    supported actionable claims       --> [choose]       --> the best one, or an open conflict
    judged claims + findings          --> [disagreements] --> Disagreement records (the report)
    unexplained dependency failures   --> [leads]        --> where the next round should look

Plain, deterministic code, and it never decides by vote: how many agents make a claim, or
how much evidence a claim cites, changes nothing. The verifier's evidence rules decide:

  1. a contradicted claim loses (one contradiction is enough);
  2. a claim must be grounded: cite at least one item its rules relied on (V1-grounding);
  3. of the supported actionable claims, the deepest root cause wins (V7-deepest-cause):
     the service the most other services' upstream errors lead to;
  4. when different root causes are still level, the conflict stays OPEN. The next round
     gets a question about it; if it is still open after that, a person decides.

Same claim = same origin, mechanism, target and detail. The detail is the bad release's
version for a bad deployment and the blamed dependency for a dependency failure, resolved
from the evidence when the claim names none. A version is written as the deployment
evidence writes it ("2.4.1" or "V2.4.1" for the observed "v2.4.1"), so one release spelled
two ways is one claim. Two versions of one service's release cannot both be the bad current
release, so they are judged apart (a detail_conflict) instead of one silently overwriting
the other: before M3 the claim seen first won, so the outcome depended on which agent's
finding came first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from typing import NamedTuple, Protocol, TypeVar

from recap.core.models import (
    AgentFinding,
    AgentRole,
    Disagreement,
    FailureMechanism,
    Hypothesis,
    HypothesisStatus,
)
from recap.tools.registry import _VERSION_RE

V7 = "V7-deepest-cause"
# The verifier's rule that judges a bad release (recap/agents/verifier.py; a test checks the
# two IDs match). It settles which of two versions of a release is the bad one.
RELEASE_RULE = "V2-deploy-timing"
SUPPORTED, CONTRADICTED = HypothesisStatus.SUPPORTED, HypothesisStatus.CONTRADICTED
# How an agent ends without any usable view of its own (see AgentFinding.outcome).
NO_VIEW = ("error", "no_usable_answer")
# Only values of these shapes are repeated in a question or a lead for the next round,
# which a model reads as trusted text: a service name (a DNS label, as in Kubernetes) and a
# release version in the tool registry's own shape (vX.Y.Z, ASCII digits: the versions a
# rollback may name). Anything else came from tool output or model text and is written
# UNNAMED, even a single word: "OPERATOR_SAYS_restart_checkout" has no spaces either.
_SERVICE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_VERSION = _VERSION_RE
UNNAMED = "(unnamed)"


class Claim(NamedTuple):
    """One distinct claim, pooled across every agent that made it."""

    hypothesis: Hypothesis
    proposers: list[AgentRole]
    detail: str  # the resolved version or dependency; "" when there is none


class JudgedClaim(Protocol):
    """What the merger reads from one of the verifier's checks."""

    hypothesis: Hypothesis
    proposers: list[AgentRole]
    detail: str
    contradictions: list[tuple[str, list[str]]]  # (rule ID, evidence IDs it found against)

    @property
    def status(self) -> HypothesisStatus: ...

    @property
    def target(self) -> str: ...


J = TypeVar("J", bound=JudgedClaim)
H = TypeVar("H", bound=Hashable)


# --- merging -----------------------------------------------------------------------------------


def merge_claims(
    findings: Sequence[AgentFinding], detail_of: Callable[[Hypothesis], str]
) -> list[Claim]:
    """One entry per distinct claim, in the order first made; duplicates pool their evidence.

    `findings` come in a fixed order (by round, then by the router's agent order), never in
    the order agents happened to finish, so the result does not depend on timing either.
    A remembered claim stays apart from a live one, so it is judged on its own.
    """
    merged: dict[tuple[str, FailureMechanism, str, str], Claim] = {}
    for finding in findings:
        for h in finding.hypotheses:
            detail = detail_of(h)
            named = h.details.get("version")
            if h.mechanism == FailureMechanism.BAD_DEPLOYMENT and named and named != detail:
                # The release in the evidence's spelling (`detail_of` matched "2.4.1" to the
                # observed "v2.4.1"): the rules then read the observed release.
                h = h.model_copy(update={"details": {**h.details, "version": detail}})
            key = (h.origin, h.mechanism, h.target_service or "", detail)
            role = h.proposed_by or finding.agent_role
            if key in merged:
                first = merged[key]
                pooled = _pool(first.hypothesis, h)
                merged[key] = Claim(pooled, _union(first.proposers, [role]), detail)
            else:
                merged[key] = Claim(h, [role], detail)
    return list(merged.values())


def _pool(first: Hypothesis, other: Hypothesis) -> Hypothesis:
    """`first`, plus the evidence IDs (and any missing details) of the same claim again."""
    return first.model_copy(
        update={
            "supporting_evidence_ids": _union(
                first.supporting_evidence_ids, other.supporting_evidence_ids
            ),
            "contradicting_evidence_ids": _union(
                first.contradicting_evidence_ids, other.contradicting_evidence_ids
            ),
            "details": {**other.details, **first.details},
        }
    )


# --- choosing between supported root causes ----------------------------------------------------


def choose(
    candidates: Sequence[J], depth: Mapping[str, tuple[int, list[str]]]
) -> tuple[J | None, list[J], Disagreement | None]:
    """V7: the deepest supported root cause, or None when different root causes are level.

    `candidates` are the supported actionable claims in merge order; `depth` maps a target
    to how many services' upstream errors lead to it, with the evidence IDs of those lines.
    Returns the best claim, every candidate deepest first (live before remembered, then in
    merge order), and a competing_root_causes record when they name two or more different
    root causes: settled by V7 when one is strictly deepest, open otherwise.
    """

    def level(claim: J) -> int:
        return depth.get(claim.target, (0, []))[0]

    ranked = sorted(candidates, key=lambda c: (-level(c), c.hypothesis.origin == "memory"))
    causes = _union([_cause(c) for c in ranked])
    if not ranked:
        return None, ranked, None
    top = level(ranked[0])
    level_claims = [c for c in ranked if level(c) == top]
    level_causes = _union([_cause(c) for c in level_claims])
    if len(level_causes) == 1:
        best = ranked[0]
        if len(causes) == 1:
            return best, ranked, None  # one root cause, however many agents proposed it
        others = [label_of(next(c for c in ranked if _cause(c) == cause)) for cause in causes[1:]]
        services = "service" if top == 1 else "services"
        record = Disagreement(
            kind="competing_root_causes",
            claims=[label_of(best), *others],
            agents=_union(*(c.proposers for c in ranked)),  # every cause is named above
            evidence_ids=depth.get(best.target, (0, []))[1],
            resolved=True,
            rule_id=V7,
            resolution=(
                f"{label_of(best)} is the deepest root cause: the upstream errors of {top} "
                f"other {services} lead to {_service(best.target)}, more than to "
                f"{_joined([_service(c.target) for c in ranked if _cause(c) != _cause(best)])}"
            ),
        )
        return best, ranked, record
    claims = [label_of(next(c for c in ranked if _cause(c) == cause)) for cause in level_causes]
    # Only the level causes are in this conflict: a shallower supported cause is not, so its
    # agents and evidence are not part of what is disputed.
    record = Disagreement(
        kind="competing_root_causes",
        claims=claims,
        agents=_union(*(c.proposers for c in level_claims)),
        evidence_ids=_union(*(c.hypothesis.supporting_evidence_ids for c in level_claims)),
        resolved=False,
        resolution=(
            f"{_joined(claims)} are all supported and equally deep, and no evidence rule "
            "decides between them"
        ),
        question=(
            f"Supported root causes compete: {_joined(claims)}. Find evidence that tells them "
            "apart: which problem began first, and whether one service's errors come from "
            "the other."
        ),
    )
    return None, ranked, record


def _cause(claim: JudgedClaim) -> tuple[FailureMechanism, str, str]:
    """The root cause a claim names, whoever proposed it and whether it was remembered."""
    return claim.hypothesis.mechanism, claim.target, claim.detail


# --- disagreements -----------------------------------------------------------------------------


def disagreements(
    checks: Sequence[JudgedClaim],
    findings: Sequence[AgentFinding],
    recorded_by: Mapping[str, AgentRole | None],
    competition: Disagreement | None,
    *,
    diagnosed: bool,
) -> list[Disagreement]:
    """Every disagreement of this verification, in a fixed order.

    `recorded_by` maps an evidence ID to the agent whose tool call recorded it.
    """
    found = [*_detail_conflicts(checks), *_cross_agent(checks, recorded_by)]
    if competition is not None:
        found.append(competition)
    return found + _missing_views(findings, diagnosed)


def _detail_conflicts(checks: Sequence[JudgedClaim]) -> list[Disagreement]:
    """Bad-release claims on one service that name different versions: one at most is right.

    Settled when exactly one version is supported (by V2, the rule that tells releases
    apart), or when the rules leave at most one version that is not contradicted (by the
    rules that contradicted the others: the target's own health or its upstream errors
    reject every release alike); open otherwise.
    """
    groups: dict[str, list[JudgedClaim]] = {}
    for check in checks:
        if check.hypothesis.mechanism == FailureMechanism.BAD_DEPLOYMENT and check.detail:
            groups.setdefault(check.target, []).append(check)
    found = []
    for target, group in groups.items():
        versions = _union([c.detail for c in group])
        if len(versions) < 2:
            continue
        supported = _union([c.detail for c in group if c.status == SUPPORTED])
        possible = supported or _union([c.detail for c in group if c.status != CONTRADICTED])
        settled = len(possible) <= 1
        service, named = _service(target), _joined([_version(v) for v in versions])
        if supported and settled:
            outcome = f"only {_version(supported[0])} is supported"
        elif settled:
            outcome = (
                f"only {_version(possible[0])} is still possible"
                if possible
                else ("every one is contradicted")
            )
        else:
            outcome = "the rules rule out none of them"
        losers = [c for c in group if c.status == CONTRADICTED]
        rejected_by = _union([rule for c in losers for rule, _ in c.contradictions])
        settled_by = RELEASE_RULE if supported else ", ".join(rejected_by)
        found.append(
            Disagreement(
                kind="detail_conflict",
                claims=[label_of(c) for c in group],
                agents=_union(*(c.proposers for c in group)),
                evidence_ids=_union(*(ids for c in losers for _, ids in c.contradictions)),
                resolved=settled,
                rule_id=settled_by if settled else "",
                resolution=f"claims name different bad releases of {service} ({named}); {outcome}",
                question=""
                if settled
                else (
                    f"Claims disagree on which release of {service} is bad ({named}). Check "
                    f"{service}'s deployment history and when its errors began."
                ),
            )
        )
    return found


def _cross_agent(
    checks: Sequence[JudgedClaim], recorded_by: Mapping[str, AgentRole | None]
) -> list[Disagreement]:
    """A claim the rules rejected with evidence that ANOTHER agent recorded."""
    found = []
    for check in checks:
        if check.status != CONTRADICTED:
            continue
        for rule, ids in check.contradictions:
            foreign = [i for i in ids if recorded_by.get(i) not in (None, *check.proposers)]
            if not foreign:
                continue
            others = _union([role for i in foreign if (role := recorded_by.get(i)) is not None])
            found.append(
                Disagreement(
                    kind="cross_agent_contradiction",
                    claims=[label_of(check)],
                    agents=_union(check.proposers, others),
                    evidence_ids=foreign,
                    resolved=True,
                    rule_id=rule,
                    resolution=(
                        f"{label_of(check)} loses: {rule} found evidence against it in what "
                        f"{_agents(others)} recorded"
                    ),
                )
            )
            break  # one record per rejected claim: the first contradiction is enough
    return found


def _missing_views(findings: Sequence[AgentFinding], diagnosed: bool) -> list[Disagreement]:
    """An agent of this epoch that made no claim and ended its last turn without a view."""
    last: dict[AgentRole, AgentFinding] = {}
    claimed: set[AgentRole] = set()
    for finding in findings:
        last[finding.agent_role] = finding
        if finding.hypotheses:
            claimed.add(finding.agent_role)
    found = []
    for role, finding in last.items():
        if role in claimed or finding.outcome not in NO_VIEW:
            continue
        rest = (
            "the diagnosis rests on the evidence the other agents recorded"
            if diagnosed
            else "nothing is supported without it"
        )
        found.append(
            Disagreement(
                kind="missing_view",
                claims=[],
                agents=[role],
                resolved=diagnosed,
                resolution=f"the {_role(role)} view is missing ({finding.outcome}); {rest}",
            )
        )
    return found


# --- leads -------------------------------------------------------------------------------------


def leads(unexplained: Sequence[JudgedClaim], checks: Sequence[JudgedClaim]) -> list[str]:
    """Where the next round should look although no claims conflict (trusted text).

    `unexplained` are the supported dependency failures (S's calls to D fail, D being the
    claim's detail) that no supported root cause on D or below it explains (V4). When no
    claim on D itself is supported either, the trail of failed calls ends at D so far, and
    the next round should look at D: one lead per D, naming every such S, both in
    alphabetical order. (When D's own calls are confirmed to fail, the trail goes on, and the
    lead names the service after D.) Only names shaped like a service name are repeated: a
    pair that fails the check makes no lead. A lead is not a disagreement: it settles nothing
    and changes no claim.
    """
    confirmed = {c.target for c in checks if c.status == SUPPORTED}
    callers: dict[str, set[str]] = {}
    for claim in unexplained:
        caller, dependency = claim.target, claim.detail
        named = _SERVICE.fullmatch(caller) and _SERVICE.fullmatch(dependency)
        if named and dependency not in confirmed:
            callers.setdefault(dependency, set()).add(caller)
    found = []
    for dependency in sorted(callers):
        calls = _joined([f"{caller}'s" for caller in sorted(callers[dependency])])
        found.append(
            f"{calls} calls to {dependency} fail and nothing on {dependency} itself is "
            f"confirmed yet: find out what is wrong with {dependency} (its errors, memory, "
            "restarts and releases)."
        )
    return found


# --- labels (only checked values, see _SERVICE and _VERSION) ---------------------------------


def label_of(claim: JudgedClaim) -> str:
    """e.g. 'bad deployment on checkout (v2.4.1)' or 'dependency failure on cart (-> payment)'."""
    h = claim.hypothesis
    text = f"{h.mechanism.value.replace('_', ' ')} on {_service(claim.target)}"
    if h.mechanism == FailureMechanism.BAD_DEPLOYMENT and claim.detail:
        text += f" ({_version(claim.detail)})"
    elif h.mechanism == FailureMechanism.DEPENDENCY_FAILURE and claim.detail:
        text += f" (-> {_service(claim.detail)})"
    return text + (" [remembered]" if h.origin == "memory" else "")


def _service(value: str) -> str:
    return value if _SERVICE.fullmatch(value) else UNNAMED


def _version(value: str) -> str:
    return value if _VERSION.fullmatch(value) else UNNAMED


def _role(role: AgentRole) -> str:
    """'telemetry', 'infrastructure', 'investigator', ..."""
    return role.value.removesuffix("_investigator").replace("_", " ")


def _agents(roles: Sequence[AgentRole]) -> str:
    """'the telemetry agent', 'the telemetry and infrastructure agents'."""
    return f"the {_joined([_role(r) for r in roles])} agent{'s' if len(roles) > 1 else ''}"


def _joined(items: Sequence[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _union(*groups: Iterable[H]) -> list[H]:
    """Items of all groups, each once, in first-seen order."""
    return list(dict.fromkeys(item for group in groups for item in group))
