"""Evidence verifier: trusted rules that decide which root-cause claim the evidence supports.

Where it sits:

    findings (hypotheses citing evidence IDs) + EvidenceRegistry -> [verify] -> VerificationReport
    mitigation draft + report -> [check_proposal] -> ProposalCheck

verify merges the agents' claims first (recap/agents/merger.py: agents making the same claim
pool their evidence), judges each claim with the rules below, picks the best one and records
every disagreement between agents and how the rules settled it.

This is structural / evidence-rule validation, NOT proof of causation. Providers propose
generously (recall); this plain, trusted code decides (precision). It reads ALL tool evidence
of the run, not only what a claim cites, so one agent's evidence can contradict another
agent's claim. It parses the evidence itself. It shares only the observation models and the
signal conventions in recap/core/signals.py with the scripted policy; its rules are written
independently, so a mistake in the policy's heuristics is not repeated in the check.

    V1-structural          a target, a known mechanism, cited evidence that exists in THIS run
    V1-grounding           a supported claim cites at least one item its rules relied on
    V2-deploy-timing       bad_deployment(S, v): S's errors began -5..+30 min around its deploy
    V2-upstream-dominance  ... and fewer than half of S's ERROR lines blame a dependency that
                           is not observed healthy
    V3-resource            resource_pressure(S): OOM kills or memory >= 90% of its limit;
                           OOM log lines only corroborate (contradicted: memory stays < 70%
                           and events show no OOM kill)
    V4-dependency          dependency_failure(S -> D): S's logs show "upstream D call failed"
                           and D is not observed healthy; never actionable by itself, it
                           points the search at D
    V5-memory              a remembered claim faces the same rules (no free pass)
    V6-conflict-resolution when log TEXT disagrees with platform signals (health snapshots,
                           metrics, cluster events), the platform signals win, and the check
                           says so with the evidence IDs of both sides
    V7-deepest-cause       of the supported actionable claims, the one the most services'
                           upstream errors lead to wins; level root causes stay an OPEN
                           conflict (no vote, no tie-break by how much a claim cites)

    VP1..VP3               a fix cites real evidence, targets the diagnosis, names an action
    VP4-restore-point      a fix that restores a version restores one the evidence names (the
                           release before the bad one, or the rollback dry run's target)

V6 also covers the target itself: an actionable claim on a service the platform shows
healthy is contradicted, because there is nothing on that service to fix.

Why V6 exists (audit finding 2.2): a log line is text that a buggy library, a compromised
service or a user can write. V1 let 3 forged "OutOfMemoryError" lines prove memory pressure
on a healthy service, and forged "upstream payment call failed" lines clear the real bad
release, so RECAP restarted the wrong service. Platform signals come from the platform
itself (the orchestrator, the metrics pipeline), so they are much harder to fake.

Epochs: after a fix, `remediated` (service -> environment time when the last fix on it began)
makes the verifier ignore STATE observations of that service from before the fix: OOM kills,
memory, health snapshots, error-rate and latency points, and its own log lines. A restart
clears memory pressure, so the OOM kills it ended must not re-support the old diagnosis
(audit finding 2.6). Blame that OTHER services' fresh logs put on a fixed service still
counts: a fix on it does not prove it is fixed, so only fresh platform evidence showing it
healthy clears that blame (V6). History stays: deployments and when errors first began.

One contradiction rejects a claim; a missing piece leaves it INSUFFICIENT. The best claim is
a SUPPORTED bad_deployment or resource_pressure: the deepest root cause (V7), live before
remembered. Before M3 a tie went to the claim citing more evidence, so padding a claim with
citations could win it; two different root causes that are level now stay an open conflict
for the next round, and then for a person. Statuses are supported / contradicted /
insufficient: never "proven".
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ValidationError

from recap.agents.merger import Claim, choose, disagreements, merge_claims
from recap.context.evidence import EvidenceRegistry
from recap.core import signals
from recap.core.models import (
    AgentFinding,
    AgentRole,
    Disagreement,
    Evidence,
    FailureMechanism,
    Hypothesis,
    HypothesisCheck,
    HypothesisStatus,
    Incident,
    MitigationDraft,
    ProposalCheck,
    SourceType,
    VerificationReport,
)
from recap.core.observations import (
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricSeries,
    RollbackPlan,
)

V1, V2_TIMING, V2_UPSTREAM = "V1-structural", "V2-deploy-timing", "V2-upstream-dominance"
V1_GROUNDING = "V1-grounding"
V3, V4, V5 = "V3-resource", "V4-dependency", "V5-memory"
V6 = "V6-conflict-resolution"
PROPOSAL_RULES = (
    "VP1-evidence-exists",
    "VP2-target-matches-diagnosis",
    "VP3-action-named",
    "VP4-restore-point",
)

SUPPORTED, CONTRADICTED = HypothesisStatus.SUPPORTED, HypothesisStatus.CONTRADICTED
INSUFFICIENT = HypothesisStatus.INSUFFICIENT
ACTIONABLE = frozenset({FailureMechanism.BAD_DEPLOYMENT, FailureMechanism.RESOURCE_PRESSURE})
UPSTREAM_SHARE = 0.5  # a service is a victim when at least half of its ERROR lines blame upstream
_MECHANISM_ORDER = {mechanism: rank for rank, mechanism in enumerate(FailureMechanism)}

# The observation model behind each read tool (Evidence.data is that model, dumped as JSON).
_TOOL_MODELS: dict[str, type[BaseModel]] = {
    "get_recent_deployments": DeploymentList,
    "get_service_logs": LogQueryResult,
    "get_service_metrics": MetricSeries,
    "get_k8s_like_events": EventList,
    "get_service_health": HealthSnapshot,
    "propose_rollback": RollbackPlan,
}
# Which service a call asked about, read from its canonical source_ref (see tool_source_ref).
_SERVICE_ARG = re.compile(r"[(,]service=([^,)]+)")


class EvidenceVerifier:
    """Trusted, rule-based judge of hypotheses and fixes (structural checks, not causal proof)."""

    name = "evidence-rule-verifier-v1"

    def verify(
        self,
        findings: Sequence[AgentFinding],
        registry: EvidenceRegistry,
        incident: Incident,
        *,
        remediated: Mapping[str, datetime] | None = None,
    ) -> VerificationReport:
        """Check every hypothesis against ALL evidence of this run and pick the best one.

        `remediated` maps a service to the environment time of the last fix executed on it;
        its state observations from before that time no longer count (see "Epochs" above).
        """
        evidence = registry.all()
        observed = _Observations(evidence, remediated)
        claims = merge_claims(findings, lambda h: _claim_detail(h, observed))
        # Observation timestamps are UTC once parsed; the alert time may still be naive.
        alert_time = signals.as_utc(incident.created_at)
        checks = [_evaluate(claim, registry, observed, alert_time) for claim in claims]
        graph = observed.upstream_graph()
        candidates = [
            c for c in checks if c.status == SUPPORTED and c.hypothesis.mechanism in ACTIONABLE
        ]
        depth = {c.target: _depth(c.target, observed, graph) for c in candidates}
        chosen, ranked, competition = choose(candidates, depth)
        _resolve_dependencies(checks, ranked, observed, graph)
        best = chosen.result().hypothesis if chosen else None
        status = _report_status(best, checks)
        open_conflict = competition if competition and not competition.resolved else None
        focus = [] if best else _focus(checks, incident, open_conflict, ranked)
        recorded_by = {item.id: item.agent_role for item in evidence}
        return VerificationReport(
            status=status,
            best=best,
            checks=[check.result() for check in checks],
            unknown_evidence_ids=_union(*(check.unknown_ids for check in checks)),
            merged_evidence_ids=_union(*(finding.evidence_ids for finding in findings)),
            focus_services=focus,
            summary=_summary(status, best, checks, focus, open_conflict),
            disagreements=disagreements(
                checks, findings, recorded_by, competition, diagnosed=best is not None
            ),
        )

    def check_proposal(
        self, proposal: MitigationDraft, report: VerificationReport, registry: EvidenceRegistry
    ) -> ProposalCheck:
        """A fix must cite real evidence, target the diagnosed service and name an action.

        Whether the action is risky is not decided here: that is the safety policy's job.
        """
        problems: list[str] = []
        unknown = [eid for eid in proposal.evidence_ids if not registry.exists(eid)]
        diagnosed = report.best.target_service if report.best else None
        if not proposal.evidence_ids:
            problems.append("the proposal cites no evidence")
        if unknown:
            problems.append(f"the proposal cites evidence not in this run: {', '.join(unknown)}")
        if diagnosed is None:
            problems.append("there is no supported diagnosis to act on")
        elif proposal.target != diagnosed:
            mismatch = f"the proposal targets {proposal.target}, but the diagnosis is {diagnosed}"
            problems.append(mismatch)
        if not proposal.action.strip():
            problems.append("the proposal names no action")
        restore = proposal.params.get("to_version")
        if restore is not None and diagnosed is not None and proposal.target == diagnosed:
            problems += _restore_point_problems(str(restore), proposal.target, report, registry)
        return ProposalCheck(ok=not problems, problems=problems, rule_ids=list(PROPOSAL_RULES))


# --- the run's evidence, parsed back into observation models ------------------------------------


class _Memory(NamedTuple):
    peak: float  # highest reading as a share of the container limit
    limit_mb: float
    evidence_ids: list[str]


class _Observations:
    """Every live tool observation of the run, each kept next to its evidence ID.

    The observation models store every timestamp in UTC (one without an offset is read as
    UTC), so the rules below can compare timestamps from different tools directly.
    """

    def __init__(
        self, evidence: Iterable[Evidence], remediated: Mapping[str, datetime] | None = None
    ) -> None:
        # service -> UTC time of the last fix on it: older state observations are stale.
        self.remediated = {s: signals.as_utc(at) for s, at in (remediated or {}).items()}
        self.deployments: list[tuple[str, DeploymentRecord]] = []
        self.logs: list[tuple[str, LogQueryResult]] = []
        self.metrics: list[tuple[str, MetricSeries]] = []
        self.events: list[tuple[str, str | None, EventList]] = []  # (id, service asked, list)
        self.health: list[tuple[str, HealthSnapshot]] = []
        self.rollback_plans: list[tuple[str, RollbackPlan]] = []  # propose_rollback dry runs
        for item in evidence:
            # Only live tool results count: a runbook or a memory can never prove anything.
            model = _TOOL_MODELS.get(item.tool_name or "")
            parsed = _parse(model, item.data) if item.source_type == SourceType.TOOL else None
            if isinstance(parsed, DeploymentList):
                self.deployments += [(item.id, record) for record in parsed.deployments]
            elif isinstance(parsed, LogQueryResult):
                self.logs.append((item.id, parsed))
            elif isinstance(parsed, MetricSeries):
                self.metrics.append((item.id, parsed))
            elif isinstance(parsed, EventList):
                asked = _SERVICE_ARG.search(item.source_ref)
                self.events.append((item.id, asked.group(1) if asked else None, parsed))
            elif isinstance(parsed, HealthSnapshot):
                self.health.append((item.id, parsed))
            elif isinstance(parsed, RollbackPlan):
                self.rollback_plans.append((item.id, parsed))

    def fresh(self, service: str, moment: datetime) -> bool:
        """True if an observation of `service` at `moment` is newer than its last fix."""
        fixed_at = self.remediated.get(service)
        return fixed_at is None or moment > fixed_at

    def release(self, service: str, version: str | None) -> tuple[str, DeploymentRecord] | None:
        """The newest deployment of `service` (of `version`, when given) and its evidence ID."""
        found = [
            (eid, record)
            for eid, record in self.deployments
            if record.service == service and version in (None, record.version)
        ]
        return max(found, key=lambda pair: pair[1].deployed_at, default=None)

    def series(self, service: str, metric: str) -> list[tuple[str, MetricSeries]]:
        """Every `metric` series of `service`, whole (history: used for error onsets)."""
        return [(eid, s) for eid, s in self.metrics if s.service == service and s.metric == metric]

    def fresh_series(self, service: str, metric: str) -> list[tuple[str, MetricSeries]]:
        """The same series, keeping only points newer than the service's last fix (state)."""
        found = []
        for eid, series in self.series(service, metric):
            points = [point for point in series.points if self.fresh(service, point.timestamp)]
            if points:
                found.append((eid, series.model_copy(update={"points": points})))
        return found

    def log_ids(self, service: str) -> list[str]:
        return [eid for eid, result in self.logs if result.service == service]

    def lines(self, service: str) -> list[tuple[str, LogLine]]:
        """`service`'s log lines, oldest first. A line seen by two log queries counts once."""
        unique: dict[tuple[datetime, str, str], tuple[str, LogLine]] = {}
        for eid, result in self.logs:
            if result.service == service:
                for line in result.lines:
                    unique.setdefault((line.timestamp, line.level, line.message), (eid, line))
        return sorted(unique.values(), key=lambda pair: pair[1].timestamp)

    def fresh_lines(self, service: str) -> list[tuple[str, LogLine]]:
        """`service`'s log lines newer than its last fix (all of them if it was never fixed)."""
        return [(eid, ln) for eid, ln in self.lines(service) if self.fresh(service, ln.timestamp)]

    def upstream_errors(self, service: str) -> list[tuple[str, str]]:
        """(evidence ID, dependency) for each ERROR line of `service` saying 'upstream D failed'.

        Only lines newer than `service`'s own last fix count. Blame toward D is kept even if a
        fix ran on D: a new epoch starts only when the oracle said "not recovered", so a fix
        on D does not mean D is fixed. Blame stops counting only when fresh platform evidence
        shows D healthy (V6, see healthy_evidence).
        """
        found = []
        for eid, line in self.fresh_lines(service):
            dependency = signals.upstream_dependency(line.message)
            if line.level == "ERROR" and dependency and dependency != service:
                found.append((eid, dependency))
        return found

    def upstream_graph(self) -> dict[str, set[str]]:
        """Caller -> the dependencies its ERROR lines blame: who points at whom.

        Blame toward a dependency the platform shows healthy is left out (V6): text alone
        must not make a healthy service look like the deepest root cause.
        """
        callers = sorted({result.service for _, result in self.logs})
        return {
            caller: {
                dep for _, dep in self.upstream_errors(caller) if not self.healthy_evidence(dep)
            }
            for caller in callers
        }

    def healthy_evidence(self, service: str) -> list[str]:
        """IDs of platform evidence that shows `service` working normally; [] if it does not.

        "Observed healthy" needs at least one health snapshot or error_rate series for the
        service, and EVERY observation must look healthy: snapshots Running with all replicas
        ready, an error rate at or below ERROR_RATE_THRESHOLD and p95 latency at or below
        LATENCY_SLO_MS; no error_rate point above its threshold; no latency point above the
        SLO (a dependency that times out is not healthy, even with few errors). Only
        observations newer than the service's last fix count. One unhealthy observation, or
        none at all, gives [].
        """
        threshold, slo = signals.ERROR_RATE_THRESHOLD, signals.LATENCY_SLO_MS
        snapshots = [
            (eid, s)
            for eid, s in self.health
            if s.service == service and self.fresh(service, s.observed_at)
        ]
        rates = self.fresh_series(service, "error_rate")
        latencies = self.fresh_series(service, "latency_p95_ms")
        if not snapshots and not rates:
            return []
        for _, snapshot in snapshots:
            ready = snapshot.replicas > 0 and snapshot.ready_replicas >= snapshot.replicas
            slow = not snapshot.latency_p95_ms <= slo  # NaN counts as not healthy
            if snapshot.status != "Running" or not ready or snapshot.error_rate > threshold or slow:
                return []
        if any(not p.value <= threshold for _, series in rates for p in series.points):
            return []
        if any(not p.value <= slo for _, series in latencies for p in series.points):
            return []
        return _union([eid for eid, _ in snapshots], [eid for eid, _ in rates])

    def oom_events(self, service: str) -> list[tuple[str, ClusterEvent]]:
        """(evidence ID, event) for OOMKilled/BackOff events of `service`, each event once."""
        unique: dict[tuple[datetime, str, str], tuple[str, ClusterEvent]] = {}
        for eid, _, listing in self.events:
            for event in listing.events:
                if (
                    event.service == service
                    and event.reason in signals.OOM_EVENT_REASONS
                    and self.fresh(service, event.timestamp)  # a fix ended the older kills
                ):
                    key = (event.timestamp, event.reason, event.object)
                    unique.setdefault(key, (eid, event))
        return list(unique.values())

    def events_covering(self, service: str) -> list[str]:
        """IDs of event lists that include `service`: asked for it, or for the whole namespace."""
        return [eid for eid, asked, _ in self.events if asked in (None, service)]

    def memory(self, service: str) -> _Memory | None:
        """Peak memory of `service` relative to its container limit, if it was measured."""
        peak, limit, evidence_ids = 0.0, 0.0, []
        for eid, series in self.fresh_series(service, "memory_mb"):
            if series.limit and series.points:  # a ratio needs a known limit
                evidence_ids.append(eid)
                limit = series.limit
                peak = max(peak, *(point.value / series.limit for point in series.points))
        return _Memory(peak, limit, evidence_ids) if evidence_ids else None


def _parse(model: type[BaseModel] | None, data: dict[str, Any]) -> BaseModel | None:
    """Validate one tool payload against its observation model; None if it does not fit."""
    if model is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None  # unreadable data is ignored: it can never support a claim


# --- what the rules found about one hypothesis --------------------------------------------------


@dataclass
class _Check:
    """One claim and every rule outcome about it (becomes a HypothesisCheck)."""

    hypothesis: Hypothesis
    proposers: list[AgentRole] = field(default_factory=list)  # every agent that made it
    detail: str = ""  # the resolved version or dependency (see merger.merge_claims)
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    contradicting_ids: list[str] = field(default_factory=list)
    # (rule ID, evidence IDs) for each contradiction, so a disagreement can name both.
    contradictions: list[tuple[str, list[str]]] = field(default_factory=list)
    basis: list[str] = field(default_factory=list)  # evidence the passing rules relied on
    unknown_ids: list[str] = field(default_factory=list)
    focus: list[str] = field(default_factory=list)  # services that deserve a closer look
    contradicted: bool = False
    missing: bool = False

    @property
    def target(self) -> str:
        return self.hypothesis.target_service or ""

    @property
    def status(self) -> HypothesisStatus:
        """A contradiction rejects the claim; a missing piece or no passed rule leaves it open."""
        if self.contradicted:
            return CONTRADICTED
        return INSUFFICIENT if self.missing or not self.passed else SUPPORTED

    def ok(self, rule: str, message: str, evidence_ids: Iterable[str] = ()) -> None:
        """A rule found what the claim needs, in `evidence_ids` (the claim's basis)."""
        self.basis = _union(self.basis, evidence_ids)
        self.note(rule, message, good=True)

    def contradict(
        self, rule: str, message: str, evidence_ids: Iterable[str], focus: Iterable[str] = ()
    ) -> None:
        """Evidence actively disagrees with the claim."""
        self.contradicted = True
        ids = _union(evidence_ids)  # one log query has many lines, but one evidence ID
        self.contradicting_ids = _union(self.contradicting_ids, ids)
        self.contradictions.append((rule, ids))
        self.note(rule, message, good=False, focus=focus)

    def lack(self, rule: str, message: str, focus: Iterable[str] = ()) -> None:
        """Evidence the rule needs is missing."""
        self.missing = True
        self.note(rule, message, good=False, focus=focus)

    def note(self, rule: str, message: str, *, good: bool, focus: Iterable[str] = ()) -> None:
        """Record one human-readable rule outcome (the dashboard shows these lines)."""
        self.rule_ids = _union(self.rule_ids, [rule])
        (self.passed if good else self.failed).append(message)
        self.focus = _union(self.focus, focus)

    def result(self) -> HypothesisCheck:
        """The contract: a copy of the hypothesis with its final status and contradictions."""
        h = self.hypothesis
        contradicting = _union(h.contradicting_evidence_ids, self.contradicting_ids)
        final = h.model_copy(
            update={"status": self.status, "contradicting_evidence_ids": contradicting}
        )
        return HypothesisCheck(
            hypothesis=final,
            passed=self.passed,
            failed=self.failed,
            rule_ids=self.rule_ids,
            proposers=self.proposers,
            basis_evidence_ids=self.basis if self.status == SUPPORTED else [],
        )


# --- the rules ----------------------------------------------------------------------------------


def _evaluate(
    claim: Claim, registry: EvidenceRegistry, observed: _Observations, alert_time: datetime
) -> _Check:
    """V1 first; the mechanism's rule only for a well-formed claim; then V1-grounding, and V5
    for remembered claims."""
    h = claim.hypothesis
    check = _Check(h, proposers=claim.proposers, detail=claim.detail)
    _structural(check, registry)
    if not check.missing:
        if h.mechanism == FailureMechanism.BAD_DEPLOYMENT:
            _bad_deployment(check, observed, alert_time)
        elif h.mechanism == FailureMechanism.RESOURCE_PRESSURE:
            _resource_pressure(check, observed)
        elif h.mechanism == FailureMechanism.DEPENDENCY_FAILURE:
            _dependency_failure(check, observed)
        if h.mechanism in ACTIONABLE:
            _healthy_target(check, observed)
        _grounding(check)
    if h.origin == "memory":
        episode = h.details.get("episode_id", "a past episode")
        confirmed = check.status == SUPPORTED
        verdict = "confirms" if confirmed else "does not confirm"
        check.note(V5, f"live evidence {verdict} what {episode} remembered", good=confirmed)
    return check


def _structural(check: _Check, registry: EvidenceRegistry) -> None:
    """V1: a checkable claim that cites evidence recorded in THIS run (no made-up IDs)."""
    h = check.hypothesis
    cited = _union(h.supporting_evidence_ids, h.contradicting_evidence_ids)
    check.unknown_ids = [eid for eid in cited if not registry.exists(eid)]
    focus = [check.target] if check.target else []
    if not check.target:
        check.lack(V1, "the claim names no target service")
    if h.mechanism == FailureMechanism.UNKNOWN:
        check.lack(V1, "the claim names no checkable failure mechanism", focus)
    if not h.supporting_evidence_ids:
        check.lack(V1, "the claim cites no evidence from this run", focus)
    if check.unknown_ids:
        unknown = ", ".join(check.unknown_ids)
        check.lack(V1, f"the claim cites evidence that is not in this run: {unknown}", focus)
    if not check.missing:
        check.ok(V1, f"every cited evidence ID exists in this run ({len(cited)} checked)")


def _healthy_target(check: _Check, observed: _Observations) -> None:
    """V6 for the target: a service the platform shows healthy is not the root cause to fix.

    For example an OOM kill 55 minutes before the alert on a service that is Running, fully
    ready and without errors through the whole incident: the kill happened, but there is
    nothing on that service to fix now. (V2 and V3 alone would still support such a claim.)
    """
    if check.contradicted:
        return
    healthy = observed.healthy_evidence(check.target)
    if healthy:
        message = (
            f"{check.target} is observed healthy ({', '.join(healthy)}): there is nothing on it "
            "to fix, so it is not this incident's root cause"
        )
        check.contradict(V6, message, healthy)


def _grounding(check: _Check) -> None:
    """V1-grounding: a supported claim must cite at least one item its rules relied on.

    The verifier reads all of the run's evidence, so a claim can be supported by evidence
    another agent recorded: that is how two specialists' views combine. But a claim citing
    none of it (only unrelated evidence) was not based on the evidence: it stays open.
    """
    if check.status != SUPPORTED:
        return
    cited = set(check.hypothesis.supporting_evidence_ids)
    used = [eid for eid in check.basis if eid in cited]
    if used:
        count = f"{len(used)} of the {len(check.basis)}"
        check.ok(V1_GROUNDING, f"the claim cites {count} evidence items its rules relied on")
        return
    relied = ", ".join(check.basis) or "none recorded"
    message = f"the claim cites none of the evidence its rules relied on ({relied})"
    check.lack(V1_GROUNDING, message, [check.target])


def _bad_deployment(check: _Check, observed: _Observations, alert_time: datetime) -> None:
    """V2: S deployed v shortly before its errors began, and those errors are S's own."""
    version = _detail(check.hypothesis, "version")
    release = observed.release(check.target, version)
    if release is None:
        named = f"{check.target} {version}" if version else check.target
        check.lack(
            V2_TIMING, f"no deployment evidence shows {named} being deployed", [check.target]
        )
    else:
        newest = observed.release(check.target, None)
        if newest is not None and newest[1].deployed_at > release[1].deployed_at:
            # e.g. a rollback already replaced the release: it cannot cause the errors now.
            replaced = (
                f"{check.target} no longer runs {release[1].version}: {newest[1].version} was "
                f"deployed {_when(newest[1].deployed_at, alert_time)}"
            )
            check.contradict(V2_TIMING, replaced, [newest[0]])
            return
        _deploy_timing(check, observed, release, alert_time)
    _upstream_dominance(check, observed)


class _Onset(NamedTuple):
    at: datetime
    start_seen: bool  # False if errors were already present at the very first observation
    evidence_ids: list[str]


def _deploy_timing(
    check: _Check,
    observed: _Observations,
    release: tuple[str, DeploymentRecord],
    alert_time: datetime,
) -> None:
    """V2 (a)-(c): the errors must begin between 5 min before and 30 min after the deploy."""
    service = check.target
    deploy_id, record = release
    when = _when(record.deployed_at, alert_time)
    check.ok(
        V2_TIMING,
        f"deployment evidence shows {service} {record.version} deployed {when}",
        [deploy_id],
    )
    onset = _error_onset(check, observed, record.version, alert_time)
    if onset is None:
        return
    low, high = signals.DEPLOY_ERROR_WINDOW_MINUTES
    delta = signals.minutes_between(record.deployed_at, onset.at)
    relative = f"{_offset(delta)} {record.version} was deployed (allowed window {low}..+{high} min)"
    evidence = [deploy_id, *onset.evidence_ids]
    if onset.start_seen and low <= delta <= high:
        check.ok(V2_TIMING, f"errors on {service} began {relative}", evidence)
    elif onset.start_seen:
        check.contradict(V2_TIMING, f"errors on {service} began {relative}", evidence)
    elif delta < low:  # the evidence starts mid-incident, yet even that is before the deploy
        check.contradict(V2_TIMING, f"{service} was already failing {relative}", evidence)
    else:
        message = f"{service} was already failing when its evidence starts: the onset is unseen"
        check.lack(V2_TIMING, message, [service])


def _error_onset(
    check: _Check, observed: _Observations, version: str, alert_time: datetime
) -> _Onset | None:
    """V2 (b): when S's errors began, from the error_rate metric, else from '(version=v)' logs."""
    service = check.target
    series = observed.series(service, "error_rate")
    if series:
        points = [(point.timestamp, point.value) for _, s in series for point in s.points]
        began = signals.error_onset(points)
        threshold = f"{signals.ERROR_RATE_THRESHOLD:.0%}"
        if began is None:
            check.lack(V2_TIMING, f"{service}'s error_rate never rises above {threshold}")
            return None
        when = _when(began, alert_time)
        check.ok(
            V2_TIMING,
            f"{service}'s error_rate is first seen above {threshold} {when}",
            [eid for eid, _ in series],
        )
        # An onset counts as seen only if the evidence also shows a calm moment before it.
        start_seen = min(timestamp for timestamp, _ in points) < began
        return _Onset(began, start_seen, [eid for eid, _ in series])
    # No metric evidence: fall back to the first ERROR line that names this release.
    lines = observed.lines(service)
    tag = f"(version={version})"
    tagged = [
        (eid, line)
        for eid, line in lines
        if line.level == "ERROR" and signals.version_tag(line.message) == version
    ]
    if not tagged:
        check.lack(V2_TIMING, f"no error_rate metric or '{tag}' errors for {service}", [service])
        return None
    eid, first = tagged[0]
    check.ok(
        V2_TIMING,
        f"{service} logs its first '{tag}' error {_when(first.timestamp, alert_time)}",
        [eid],
    )
    return _Onset(first.timestamp, lines[0][1].timestamp < first.timestamp, [eid])


def _upstream_dominance(check: _Check, observed: _Observations) -> None:
    """V2 (d): a service whose ERROR lines mostly blame a dependency is a victim, not the cause.

    Only blame toward a dependency that is NOT observed healthy counts (V6): "upstream D
    call failed" is log text, and the platform's own health signals for D outrank it.
    """
    service = check.target
    if not observed.log_ids(service):
        message = f"no log evidence for {service}: errors from its dependencies are not ruled out"
        check.lack(V2_UPSTREAM, message, [service])
        return
    fresh = observed.fresh_lines(service)
    if not fresh:  # every log line of the service is older than its last fix
        message = f"no log lines of {service} since its last fix: its errors cannot be attributed"
        check.lack(V2_UPSTREAM, message, [service])
        return
    errors = sum(1 for _, line in fresh if line.level == "ERROR")
    upstream = _discount_healthy_blame(check, observed, observed.upstream_errors(service))
    counted = f"{len(upstream)} of {errors} ERROR lines of {service}"
    if len(upstream) < UPSTREAM_SHARE * errors or not upstream:
        check.ok(
            V2_UPSTREAM,
            f"{counted} blame a dependency (under {UPSTREAM_SHARE:.0%})",
            [eid for eid, _ in fresh],
        )
        return
    blamed = _union([dependency for _, dependency in upstream])
    message = f"{counted} say 'upstream ... call failed': its errors come from {', '.join(blamed)}"
    check.contradict(V2_UPSTREAM, message, [eid for eid, _ in upstream], focus=blamed)


def _discount_healthy_blame(
    check: _Check, observed: _Observations, upstream: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """V6 for V2: drop blame toward dependencies the platform shows healthy, and say so."""
    kept: list[tuple[str, str]] = []
    dropped: dict[str, list[str]] = {}  # healthy dependency -> evidence IDs of the blame lines
    for eid, dependency in upstream:
        if observed.healthy_evidence(dependency):
            dropped.setdefault(dependency, []).append(eid)
        else:
            kept.append((eid, dependency))
    for dependency, line_ids in dropped.items():
        proof = ", ".join(observed.healthy_evidence(dependency))
        check.ok(
            V6,
            f"{len(line_ids)} ERROR lines of {check.target} blame {dependency}, but {dependency} "
            f"is observed healthy ({proof}); platform signals outrank log text, so they are "
            f"not counted (lines in {', '.join(_union(line_ids))})",
        )
    return kept


def _resource_pressure(check: _Check, observed: _Observations) -> None:
    """V3: platform signals show memory trouble: OOM kills or memory at the limit.

    Out-of-memory LOG LINES only corroborate. On their own they leave the claim INSUFFICIENT,
    and when platform signals disagree with them the claim is CONTRADICTED (V6).
    """
    service = check.target
    oom_events = observed.oom_events(service)
    memory = observed.memory(service)
    near_limit = memory is not None and memory.peak >= signals.MEMORY_PRESSURE_RATIO
    oom_lines = [
        eid for eid, ln in observed.fresh_lines(service) if signals.is_oom_message(ln.message)
    ]
    if oom_events:
        events = [eid for eid, _ in oom_events]
        check.ok(V3, f"{len(oom_events)} OOMKilled/BackOff events for {service}", events)
    if memory is not None and near_limit:
        limit = f"{memory.limit_mb:g} MB limit (pressure from {signals.MEMORY_PRESSURE_RATIO:.0%})"
        message = f"{service}'s memory reached {memory.peak:.0%} of its {limit}"
        check.ok(V3, message, memory.evidence_ids)
    if oom_events or near_limit:
        if oom_lines:  # text that agrees with a platform signal adds corroboration
            check.ok(V3, f"{len(oom_lines)} out-of-memory errors in {service}'s logs", oom_lines)
        return  # a platform signal is enough
    # An event list cannot be dated, so for a service that was fixed in this run, a list read
    # before the fix must not prove "no OOM kills": coverage then counts as missing.
    fixed = service in observed.remediated
    covering = [] if fixed else observed.events_covering(service)
    relaxed = signals.MEMORY_RELAXED_RATIO
    if memory is not None and memory.peak < relaxed and covering:
        message = (
            f"{service}'s memory peaks at {memory.peak:.0%} of its limit (under {relaxed:.0%}) "
            "and the cluster events show no OOMKilled/BackOff for it"
        )
        check.contradict(V3, message, [*memory.evidence_ids, *covering])
        if oom_lines:
            _text_loses(check, oom_lines, [*memory.evidence_ids, *covering])
        return
    healthy = observed.healthy_evidence(service)
    if oom_lines and healthy:
        # The platform reports a service that runs normally: text alone cannot outweigh it.
        message = (
            f"{len(oom_lines)} log lines of {service} claim out-of-memory, but health and "
            "error-rate evidence show it running normally"
        )
        check.contradict(V3, message, healthy)
        _text_loses(check, oom_lines, healthy)
        return
    if oom_lines:
        message = (
            f"only log text claims out-of-memory on {service} ({len(oom_lines)} lines); a memory "
            "metric near its limit or OOMKilled/BackOff events must confirm it"
        )
        check.lack(V3, message, [service])
        return
    gaps = [f"memory peaks at {memory.peak:.0%} of its limit" if memory else "no memory metric"]
    if not covering:
        gaps.append(
            "no cluster events since its last fix" if fixed else "no cluster events cover it"
        )
    message = f"cannot confirm or rule out memory pressure on {service}: {', '.join(gaps)}"
    check.lack(V3, message, [service])


def _text_loses(check: _Check, text_ids: Sequence[str], platform_ids: Sequence[str]) -> None:
    """V6: record that log text lost to platform signals, naming the evidence on both sides."""
    check.note(
        V6,
        f"log text ({', '.join(_union(text_ids))}) conflicts with platform signals "
        f"({', '.join(_union(platform_ids))}); the platform signals win",
        good=False,
    )


def _dependency_failure(check: _Check, observed: _Observations) -> None:
    """V4 (first half): S's own logs must show its calls to dependency D failing."""
    service = check.target
    if not observed.log_ids(service):
        check.lack(V4, f"no log evidence for {service}", [service])
        return
    if not observed.fresh_lines(service):  # every log line of it is older than its last fix
        check.lack(V4, f"no log lines of {service} since its last fix", [service])
        return
    dependency = _named_dependency(check.hypothesis, observed)
    failures = [eid for eid, dep in observed.upstream_errors(service) if dep == dependency]
    if not failures:
        message = f"{service}'s logs show no 'upstream {dependency or '...'} call failed' errors"
        check.contradict(V4, message, observed.log_ids(service))
        return
    quote = f"'upstream {dependency} call failed'"
    healthy = observed.healthy_evidence(dependency or "")
    if healthy:
        message = (
            f"{len(failures)} ERROR lines of {service} say {quote}, but {dependency} is "
            "observed healthy"
        )
        check.contradict(V4, message, healthy)
        _text_loses(check, failures, healthy)
        return
    check.ok(V4, f"{len(failures)} ERROR lines of {service} say {quote}", failures)


def _resolve_dependencies(
    checks: Sequence[_Check],
    candidates: Sequence[_Check],
    observed: _Observations,
    graph: Mapping[str, set[str]],
) -> None:
    """V4 (second half): a dependency failure needs a supported root cause on D or below it."""
    for check in checks:
        h = check.hypothesis
        if h.mechanism != FailureMechanism.DEPENDENCY_FAILURE or check.status != SUPPORTED:
            continue
        dependency = _named_dependency(h, observed) or ""
        below = {dependency, *_reachable(dependency, graph)}
        cause = next((c for c in candidates if c.target in below), None)
        if cause is None:
            message = f"not actionable by itself, and nothing on {dependency} is supported yet"
            check.note(V4, message, good=False, focus=[dependency])
        else:
            check.ok(V4, f"explained by {_words(cause.hypothesis.mechanism)} on {cause.target}")


# --- choosing the best hypothesis and writing the report ----------------------------------------


def _claim_detail(h: Hypothesis, observed: _Observations) -> str:
    """The detail that tells two claims of one mechanism on one service apart.

    A bad deployment: the bad release's version (the newest observed release of the target
    when the claim names none, as V2 reads it). A dependency failure: the blamed dependency
    (the first one the target's own logs blame when the claim names none, as V4 reads it).
    """
    target = h.target_service or ""
    if h.mechanism == FailureMechanism.BAD_DEPLOYMENT:
        release = observed.release(target, None)
        return _detail(h, "version") or (release[1].version if release else "")
    if h.mechanism == FailureMechanism.DEPENDENCY_FAILURE:
        return _named_dependency(h, observed) or ""
    return ""


def _depth(
    target: str, observed: _Observations, graph: Mapping[str, set[str]]
) -> tuple[int, list[str]]:
    """V7: how many services' upstream errors lead to `target`, and the IDs of those lines.

    A caller counts when its blame leads to `target` directly or through other services.
    Blame toward a dependency observed healthy is not in the graph (V6), so text alone
    cannot make a healthy service look like the deepest root cause.
    """
    callers = [caller for caller in graph if target in _reachable(caller, graph)]
    lines = [
        eid
        for caller in callers
        for eid, dependency in observed.upstream_errors(caller)
        if dependency in graph.get(caller, set())
        and (dependency == target or target in _reachable(dependency, graph))
    ]
    return len(callers), _union(lines)


def _restore_point_problems(
    version: str, target: str, report: VerificationReport, registry: EvidenceRegistry
) -> list[str]:
    """VP4: a fix that restores a version must restore one the evidence names.

    Named versions: the version before the diagnosed release in the deployment evidence,
    and the target of a propose_rollback dry run for the service. Restoring the release
    the diagnosis blames is never right.
    """
    observed = _Observations(registry.all())
    best = report.best
    bad = None
    if best is not None and best.mechanism == FailureMechanism.BAD_DEPLOYMENT:
        bad = _claim_detail(best, observed) or None
    release = observed.release(target, bad)
    earlier = []  # the release deployed just before the bad one, if the history shows it
    if release is not None:
        older = [
            record
            for _, record in observed.deployments
            if record.service == target
            and record.deployed_at < release[1].deployed_at
            and record.version != release[1].version
        ]
        earlier = [max(older, key=lambda record: record.deployed_at).version] if older else []
    named = _union(
        [release[1].previous_version] if release and release[1].previous_version else [],
        earlier,
        [
            plan.target_version
            for _, plan in observed.rollback_plans
            if plan.service == target and plan.possible and plan.target_version
        ],
    )
    if bad is not None and version == bad:
        return [f"the proposal restores {version}, the release the diagnosis blames"]
    if not named:
        return [
            f"no deployment history or rollback dry run in this run names a version of "
            f"{target} to restore"
        ]
    if version not in named:
        return [f"the proposal restores {version}, but the evidence names {', '.join(named)}"]
    return []


def _report_status(
    best: Hypothesis | None, checks: Sequence[_Check]
) -> Literal["supported", "contradicted", "insufficient"]:
    if best is not None:
        return "supported"
    if checks and all(check.status == CONTRADICTED for check in checks):
        return "contradicted"
    return "insufficient"


def _focus(
    checks: Sequence[_Check],
    incident: Incident,
    conflict: Disagreement | None = None,
    ranked: Sequence[_Check] = (),
) -> list[str]:
    """Where the next round should look: the services of an open conflict first, then
    services with missing evidence or open leads."""
    if not checks:  # nobody proposed anything: start again from the alerted services
        return _union([incident.service, *incident.affected_services])
    competing = [c.target for c in ranked] if conflict is not None else []
    return _union(competing, *(check.focus for check in checks))


def _summary(
    status: str,
    best: Hypothesis | None,
    checks: Sequence[_Check],
    focus: Sequence[str],
    conflict: Disagreement | None = None,
) -> str:
    """One plain sentence for the dashboard and the trace."""
    counts = Counter(check.status for check in checks)
    order = (SUPPORTED, CONTRADICTED, INSUFFICIENT)
    tally = ", ".join(f"{counts[s]} {s.value}" for s in order if counts[s])
    if best is not None:
        target = best.target_service
        return f"Best supported root cause: {_words(best.mechanism)} on {target} ({tally})."
    if status == "contradicted":
        return f"The evidence contradicts every hypothesis ({tally}); nothing can be acted on."
    where = ", ".join(focus) or "the alerted services"
    if conflict is not None:
        return (
            f"Supported root causes compete ({'; '.join(conflict.claims)}) and no evidence "
            f"rule decides between them ({tally}); look next at {where}."
        )
    if not checks:
        return f"No hypotheses were proposed; look next at {where}."
    return f"No actionable root cause is supported yet ({tally}); look next at {where}."


# --- small helpers ------------------------------------------------------------------------------


def _named_dependency(h: Hypothesis, observed: _Observations) -> str | None:
    """The dependency a claim blames; if it names none, the first one S's own logs blame."""
    failing = observed.upstream_errors(h.target_service or "")
    return _detail(h, "dependency") or (failing[0][1] if failing else None)


def _reachable(start: str, graph: Mapping[str, set[str]]) -> set[str]:
    """Every service reachable from `start` along `graph` edges (safe against cycles)."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        for nxt in graph.get(stack.pop(), set()):
            if nxt not in seen and nxt != start:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _detail(h: Hypothesis, key: str) -> str | None:
    value = h.details.get(key)
    return str(value) if value else None


def _union(*groups: Iterable[str]) -> list[str]:
    """Items of all groups, each once, in first-seen order."""
    return list(dict.fromkeys(item for group in groups for item in group))


def _words(mechanism: FailureMechanism) -> str:
    return mechanism.value.replace("_", " ")


def _when(moment: datetime, alert_time: datetime) -> str:
    """Time relative to the alert in plain words, e.g. '12 min before the alert'."""
    minutes = round(signals.minutes_between(moment, alert_time))
    if minutes == 0:
        return "at the alert time"
    return f"{abs(minutes)} min {'before' if minutes > 0 else 'after'} the alert"


def _offset(delta_minutes: float) -> str:
    """'89 min after' or '12 min before' (followed by 'vX was deployed')."""
    minutes = round(delta_minutes)
    return f"{abs(minutes)} min {'after' if minutes >= 0 else 'before'}"
