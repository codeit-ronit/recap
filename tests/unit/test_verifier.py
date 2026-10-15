"""Tests for the EvidenceVerifier: rules V1-V5, merging, the best choice and proposal checks.

Evidence is built the way the gateway records it: a real observation model from
recap/core/observations.py, dumped with model_dump(mode="json"), stored in an EvidenceRegistry.
Minute 0 is the alert time.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from recap.agents.verifier import EvidenceVerifier
from recap.context.evidence import EvidenceRegistry, tool_source_ref
from recap.core.interfaces import Verifier
from recap.core.models import (
    AgentFinding,
    AgentRole,
    FailureMechanism,
    Hypothesis,
    HypothesisCheck,
    HypothesisStatus,
    Incident,
    MitigationDraft,
    RunBudget,
    SourceType,
    VerificationReport,
)
from recap.core.observations import (
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    LogLine,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from recap.core.signals import RELEASE_ERROR_TEMPLATE, UPSTREAM_ERROR_TEMPLATE

ALERT = datetime(2026, 3, 16, 9, 15, tzinfo=UTC)
DEPLOY = FailureMechanism.BAD_DEPLOYMENT
PRESSURE = FailureMechanism.RESOURCE_PRESSURE
DEPENDENCY = FailureMechanism.DEPENDENCY_FAILURE
SUPPORTED = HypothesisStatus.SUPPORTED
CONTRADICTED = HypothesisStatus.CONTRADICTED
INSUFFICIENT = HypothesisStatus.INSUFFICIENT
# A UTC timestamp as pydantic writes it in JSON, e.g. "2026-03-16T09:15:00Z".
UTC_TIMESTAMP = re.compile(r'(T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z"')


def at(minute: float) -> datetime:
    return ALERT + timedelta(minutes=minute)


class Run:
    """One run's evidence registry, filled with real observation models.

    `utc_suffix` is how recorded timestamps end: "Z", "+00:00" or "" (no offset at all, like
    an adapter that leaves it out).
    """

    def __init__(self, run_id: str = "run-a1b2c3", utc_suffix: str = "Z") -> None:
        self.registry = EvidenceRegistry(run_id, "inc-1")
        self.utc_suffix = utc_suffix
        self.incident = Incident(
            incident_id="inc-1",
            scenario_id="test",
            service="checkout",
            affected_services=["checkout"],
            symptoms=["checkout: HTTP 5xx ratio above 30%"],
            created_at=ALERT,
            environment="simulated",
            budget=RunBudget(),
        )

    def observe(
        self,
        tool: str,
        observation: BaseModel,
        source: SourceType = SourceType.TOOL,
        **args: object,
    ) -> str:
        text = UTC_TIMESTAMP.sub(rf'\1{self.utc_suffix}"', observation.model_dump_json())
        data = json.loads(text)
        evidence = self.registry.add(
            source_type=source,
            source_ref=tool_source_ref(tool, args),
            content=json.dumps(data),
            data=data,
            tool_name=tool,
        )
        return evidence.id

    def deployments(self, *releases: tuple[str, str, int]) -> str:
        """(service, version, minute) records, newest first like the real tool."""
        records = [
            DeploymentRecord(service=s, version=v, deployed_at=at(m)) for s, v, m in releases
        ]
        records.sort(key=lambda record: record.deployed_at, reverse=True)
        return self.observe("get_recent_deployments", DeploymentList(deployments=records), limit=10)

    def error_rate(
        self, service: str, onset: int | None, *, start: int = -60, step: int = 5
    ) -> str:
        """error_rate from `start` to now: calm (0.2%) until `onset`, then 32%."""
        points = [
            MetricPoint(timestamp=at(m), value=0.32 if onset is not None and m >= onset else 0.002)
            for m in range(start, 1, step)
        ]
        series = MetricSeries(service=service, metric="error_rate", unit="ratio", points=points)
        return self.observe("get_service_metrics", series, service=service, metric="error_rate")

    def memory(self, service: str, ratio: float, limit: float = 512.0) -> str:
        points = [MetricPoint(timestamp=at(m), value=ratio * limit) for m in range(-60, 1, 5)]
        series = MetricSeries(
            service=service, metric="memory_mb", unit="MB", points=points, limit=limit
        )
        return self.observe("get_service_metrics", series, service=service, metric="memory_mb")

    def logs(self, service: str, errors: dict[int, str] | None = None, start: int = -60) -> str:
        """INFO traffic every 3 minutes plus ERROR lines given as {minute: message}."""
        info = f"GET /api/{service}/items 200 in 90ms"
        lines = [
            LogLine(timestamp=at(m), level="INFO", service=service, message=info)
            for m in range(start, 1, 3)
        ]
        lines += [
            LogLine(timestamp=at(m), level="ERROR", service=service, message=message)
            for m, message in (errors or {}).items()
        ]
        lines.sort(key=lambda line: line.timestamp)
        result = LogQueryResult(service=service, lines=lines, total_matched=len(lines))
        return self.observe("get_service_logs", result, service=service)

    def events(self, *events: ClusterEvent, service: str | None = None) -> str:
        args: dict[str, object] = {"service": service} if service else {"limit": 30}
        return self.observe("get_k8s_like_events", EventList(events=list(events)), **args)


def release_errors(version: str, minutes: range) -> dict[int, str]:
    message = RELEASE_ERROR_TEMPLATE.format(exception="NullPointerException", version=version)
    return dict.fromkeys(minutes, message)


def upstream_errors(dependency: str, minutes: range) -> dict[int, str]:
    message = UPSTREAM_ERROR_TEMPLATE.format(dependency=dependency, reason="timeout after 3000ms")
    return dict.fromkeys(minutes, message)


def oom_event(service: str, minute: int, reason: str = "OOMKilled") -> ClusterEvent:
    return ClusterEvent(
        timestamp=at(minute),
        type="Warning",
        reason=reason,
        object=f"pod/{service}-5d8f7-x2k9q",
        service=service,
        message=f"Container {service} exceeded its memory limit (512Mi) and was killed",
    )


def scaling_event(service: str, minute: int) -> ClusterEvent:
    return ClusterEvent(
        timestamp=at(minute),
        type="Normal",
        reason="ScalingReplicaSet",
        object=f"deployment/{service}",
        service=service,
        message=f"Scaled up replica set {service}-5d8f7 to 2",
    )


def hypothesis(
    mechanism: FailureMechanism,
    target: str,
    *evidence_ids: str,
    origin: str = "investigation",
    **details: str,
) -> Hypothesis:
    return Hypothesis(
        claim=f"{mechanism.value} on {target}",
        mechanism=mechanism,
        target_service=target,
        supporting_evidence_ids=list(evidence_ids),
        details=details,
        origin=origin,  # type: ignore[arg-type]
    )


def finding(
    *hypotheses: Hypothesis,
    role: AgentRole = AgentRole.INVESTIGATOR,
    evidence_ids: tuple[str, ...] = (),
    error: str | None = None,
) -> AgentFinding:
    return AgentFinding(
        agent_role=role,
        hypotheses=list(hypotheses),
        evidence_ids=list(evidence_ids),
        error=error,
        started_at=ALERT,
        finished_at=ALERT,
    )


def verify(run: Run, *hypotheses: Hypothesis) -> VerificationReport:
    return EvidenceVerifier().verify([finding(*hypotheses)], run.registry, run.incident)


def only_check(report: VerificationReport) -> HypothesisCheck:
    assert len(report.checks) == 1
    return report.checks[0]


def bad_checkout_release(run: Run) -> list[str]:
    """Evidence of a clear bad release: checkout v2.4.1 deployed at -12, errors from -10."""
    return [
        run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12)),
        run.error_rate("checkout", onset=-10),
        run.logs("checkout", release_errors("v2.4.1", range(-11, 1))),
    ]


# --- V2: bad deployment ---------------------------------------------------------------------------


def test_recent_bad_deployment_is_supported() -> None:
    run = Run()
    ids = bad_checkout_release(run)
    report = verify(run, hypothesis(DEPLOY, "checkout", *ids, version="v2.4.1"))

    check = only_check(report)
    assert check.hypothesis.status == SUPPORTED
    assert check.rule_ids == ["V1-structural", "V2-deploy-timing", "V2-upstream-dominance"]
    window = "(allowed window -5..+30 min)"
    assert f"errors on checkout began 2 min after v2.4.1 was deployed {window}" in check.passed
    assert report.status == "supported"
    assert report.best is not None and report.best.target_service == "checkout"
    assert report.best.status == SUPPORTED
    assert report.focus_services == []
    assert "bad deployment on checkout" in report.summary


def test_stale_deployment_is_contradicted_by_its_timing() -> None:
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.5.0", -95))
    errors = run.error_rate("checkout", onset=-6, step=2)
    run.logs("checkout", release_errors("v2.5.0", range(-6, 1)))
    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, errors, version="v2.5.0"))

    check = only_check(report)
    assert check.hypothesis.status == CONTRADICTED
    window = "(allowed window -5..+30 min)"
    assert f"errors on checkout began 89 min after v2.5.0 was deployed {window}" in check.failed
    assert check.hypothesis.contradicting_evidence_ids == [deploy, errors]
    assert report.status == "contradicted" and report.best is None


def test_errors_passed_on_by_a_dependency_contradict_a_bad_deployment() -> None:
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.1", -12))
    errors = run.error_rate("checkout", onset=-10)
    logs = run.logs("checkout", upstream_errors("payment", range(-10, 1)))
    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, errors, logs, version="v2.4.1"))

    check = only_check(report)
    assert check.hypothesis.status == CONTRADICTED
    assert any("its errors come from payment" in line for line in check.failed)
    assert logs in check.hypothesis.contradicting_evidence_ids
    assert report.focus_services == ["payment"]  # the next round looks at the dependency


def test_onset_falls_back_to_version_tagged_logs_without_a_metric() -> None:
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.1", -12))
    logs = run.logs("checkout", release_errors("v2.4.1", range(-11, 1)))
    check = only_check(verify(run, hypothesis(DEPLOY, "checkout", deploy, logs)))

    assert check.hypothesis.status == SUPPORTED  # version taken from the newest deployment
    assert (
        "checkout logs its first '(version=v2.4.1)' error 11 min before the alert" in check.passed
    )


def test_an_onset_hidden_before_the_evidence_window_is_not_guessed() -> None:
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.1", -62))
    errors = run.error_rate("checkout", onset=-200)  # already failing at the first point
    logs = run.logs("checkout", release_errors("v2.4.1", range(-30, 1)))
    check = only_check(verify(run, hypothesis(DEPLOY, "checkout", deploy, errors, logs)))

    assert check.hypothesis.status == INSUFFICIENT
    assert any("onset is unseen" in line for line in check.failed)


def test_missing_logs_leave_a_bad_deployment_insufficient() -> None:
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.1", -12))
    errors = run.error_rate("checkout", onset=-10)
    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, errors, version="v2.4.1"))

    check = only_check(report)
    assert check.hypothesis.status == INSUFFICIENT
    assert any("no log evidence for checkout" in line for line in check.failed)
    assert report.focus_services == ["checkout"]


# --- V3: resource pressure ------------------------------------------------------------------------


def test_oom_kills_support_resource_pressure() -> None:
    run = Run()
    events = run.events(oom_event("payment", -6), oom_event("payment", -6, "BackOff"))
    run.events(oom_event("payment", -6), service="payment")  # the same kill listed twice
    report = verify(run, hypothesis(PRESSURE, "payment", events))

    check = only_check(report)
    assert check.hypothesis.status == SUPPORTED
    assert "2 OOMKilled/BackOff events for payment" in check.passed
    assert report.best is not None and report.best.mechanism == PRESSURE


def test_relaxed_memory_without_oom_events_contradicts_resource_pressure() -> None:
    run = Run()
    memory = run.memory("payment", ratio=0.40)
    events = run.events(scaling_event("payment", -30), service="payment")
    check = only_check(verify(run, hypothesis(PRESSURE, "payment", memory)))

    assert check.hypothesis.status == CONTRADICTED
    assert any("peaks at 40% of its limit" in line for line in check.failed)
    assert check.hypothesis.contradicting_evidence_ids == [memory, events]


def test_relaxed_memory_without_event_coverage_is_only_insufficient() -> None:
    run = Run()
    memory = run.memory("payment", ratio=0.40)
    run.events(scaling_event("cart", -30), service="cart")  # does not cover payment
    check = only_check(verify(run, hypothesis(PRESSURE, "payment", memory)))

    assert check.hypothesis.status == INSUFFICIENT
    assert any("no cluster events cover it" in line for line in check.failed)


def test_runbook_or_memory_text_never_counts_as_live_evidence() -> None:
    run = Run()
    events = EventList(events=[oom_event("payment", -6)])
    remembered = run.observe("get_k8s_like_events", events, source=SourceType.MEMORY)
    check = only_check(verify(run, hypothesis(PRESSURE, "payment", remembered)))

    assert check.hypothesis.status == INSUFFICIENT


# --- V4: dependency failure -----------------------------------------------------------------------


def test_a_dependency_failure_alone_points_at_the_uninvestigated_dependency() -> None:
    run = Run()
    logs = run.logs("checkout", upstream_errors("payment", range(-6, 1)))
    report = verify(run, hypothesis(DEPENDENCY, "checkout", logs, dependency="payment"))

    check = only_check(report)
    assert check.hypothesis.status == SUPPORTED  # the logs really show it...
    assert report.status == "insufficient" and report.best is None  # ...but it is no fix target
    assert report.focus_services == ["payment"]
    assert any("not actionable by itself" in line for line in check.failed)


def test_dependency_chain_resolves_to_the_deepest_supported_root_cause() -> None:
    run = Run()
    frontend_logs = run.logs("frontend", upstream_errors("checkout", range(-4, 1)))
    checkout_logs = run.logs("checkout", upstream_errors("payment", range(-4, 1)))
    checkout_oom = run.events(oom_event("checkout", -2), service="checkout")
    payment = [
        run.deployments(("payment", "v1.12.3", -3600), ("payment", "v1.13.0", -9)),
        run.error_rate("payment", onset=-5),
        run.logs("payment", release_errors("v1.13.0", range(-8, 1))),
    ]
    report = verify(
        run,
        hypothesis(DEPENDENCY, "frontend", frontend_logs, dependency="checkout"),
        hypothesis(DEPENDENCY, "checkout", checkout_logs, dependency="payment"),
        hypothesis(PRESSURE, "checkout", checkout_oom),  # supported, but a victim higher up
        hypothesis(DEPLOY, "payment", *payment, version="v1.13.0"),
    )

    assert report.status == "supported"
    assert report.best is not None
    assert (report.best.mechanism, report.best.target_service) == (DEPLOY, "payment")
    for check in report.checks:
        if check.hypothesis.mechanism == DEPENDENCY:
            assert "explained by bad deployment on payment" in check.passed


def test_a_dependency_claim_the_logs_do_not_show_is_contradicted() -> None:
    run = Run()
    logs = run.logs("checkout", release_errors("v2.4.1", range(-5, 1)))
    check = only_check(verify(run, hypothesis(DEPENDENCY, "checkout", logs, dependency="cart")))

    assert check.hypothesis.status == CONTRADICTED


# --- V1 and V5: structure and memory --------------------------------------------------------------


def test_unknown_or_other_run_evidence_ids_make_a_claim_insufficient() -> None:
    run, other_run = Run(), Run("run-ffffff")
    foreign = other_run.logs("checkout")  # real evidence, but from another run
    ids = [*bad_checkout_release(run), foreign, "ev-made-up-1"]
    report = verify(run, hypothesis(DEPLOY, "checkout", *ids, version="v2.4.1"))

    check = only_check(report)
    assert check.hypothesis.status == INSUFFICIENT
    assert report.unknown_evidence_ids == [foreign, "ev-made-up-1"]
    assert check.rule_ids == ["V1-structural"]  # an ill-formed claim is not checked further
    assert report.status == "insufficient"


def test_a_remembered_diagnosis_gets_no_free_pass() -> None:
    run = Run()  # like memory-caution: checkout's release is old, payment is out of memory
    deploy = run.deployments(("checkout", "v2.5.0", -95))
    errors = run.error_rate("checkout", onset=-5)
    logs = run.logs("checkout", upstream_errors("payment", range(-6, 1)))
    remembered = hypothesis(
        DEPLOY, "checkout", deploy, errors, logs, origin="memory", episode_id="ep-past"
    )
    check = only_check(verify(run, remembered))

    assert check.hypothesis.status == CONTRADICTED
    assert "V5-memory" in check.rule_ids
    assert "live evidence does not confirm what ep-past remembered" in check.failed


def test_a_remembered_diagnosis_confirmed_by_live_evidence_is_supported() -> None:
    run = Run()
    ids = bad_checkout_release(run)
    check = only_check(verify(run, hypothesis(DEPLOY, "checkout", *ids, origin="memory")))

    assert check.hypothesis.status == SUPPORTED
    assert any("confirms" in line for line in check.passed)


# --- merging, report status and determinism -------------------------------------------------------


def test_duplicate_claims_from_two_agents_are_merged_and_live_beats_memory() -> None:
    run = Run()
    deploy = run.deployments(("payment", "v1.13.0", -9))
    errors = run.error_rate("payment", onset=-5)
    logs = run.logs("payment", release_errors("v1.13.0", range(-8, 1)))
    telemetry = finding(
        hypothesis(DEPLOY, "payment", errors, logs, version="v1.13.0"),
        role=AgentRole.TELEMETRY,
        evidence_ids=(errors, logs),
    )
    infrastructure = finding(
        hypothesis(DEPLOY, "payment", deploy, version="v1.13.0"),
        hypothesis(DEPLOY, "payment", errors, logs, deploy, origin="memory"),
        role=AgentRole.INFRASTRUCTURE,
        evidence_ids=(deploy,),
    )
    report = EvidenceVerifier().verify([telemetry, infrastructure], run.registry, run.incident)

    live = [c for c in report.checks if c.hypothesis.origin == "investigation"]
    assert len(report.checks) == 2  # the remembered claim is judged on its own
    assert live[0].hypothesis.supporting_evidence_ids == [errors, logs, deploy]
    assert report.merged_evidence_ids == [errors, logs, deploy]
    assert report.best is not None and report.best.origin == "investigation"


def test_no_hypotheses_means_insufficient_and_look_at_the_alerted_services() -> None:
    run = Run()
    failed_agent = finding(error="timeout after 60s")
    report = EvidenceVerifier().verify([failed_agent], run.registry, run.incident)

    assert report.status == "insufficient" and report.checks == []
    assert report.focus_services == ["checkout"]


def test_verification_is_deterministic() -> None:
    run = Run()
    ids = bad_checkout_release(run)
    claims = [hypothesis(DEPLOY, "checkout", *ids), hypothesis(PRESSURE, "checkout", ids[1])]
    first, second = verify(run, *claims), verify(run, *claims)

    assert first.model_dump() == second.model_dump()


# --- proposal checks ------------------------------------------------------------------------------


def test_check_proposal_catches_unknown_evidence_and_a_wrong_target() -> None:
    run = Run()
    ids = bad_checkout_release(run)
    report = verify(run, hypothesis(DEPLOY, "checkout", *ids, version="v2.4.1"))
    verifier = EvidenceVerifier()
    good = MitigationDraft(
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="roll back the bad release",
        evidence_ids=ids,
    )
    assert verifier.check_proposal(good, report, run.registry).ok

    bad = good.model_copy(update={"target": "payment", "evidence_ids": [ids[0], "ev-nowhere"]})
    check = verifier.check_proposal(bad, report, run.registry)
    assert not check.ok
    assert any("ev-nowhere" in problem for problem in check.problems)
    assert any("targets payment" in problem for problem in check.problems)
    assert check.rule_ids == [
        "VP1-evidence-exists",
        "VP2-target-matches-diagnosis",
        "VP3-action-named",
    ]


def test_check_proposal_needs_evidence_an_action_and_a_diagnosis() -> None:
    run = Run()
    empty = MitigationDraft(action=" ", target="checkout", rationale="?")
    no_diagnosis = VerificationReport(status="insufficient")
    check = EvidenceVerifier().check_proposal(empty, no_diagnosis, run.registry)

    assert not check.ok
    assert len(check.problems) == 3  # no evidence, no diagnosis, no action


# --- timestamps and the Verifier interface --------------------------------------------------------


def test_timestamps_without_an_offset_are_read_as_utc() -> None:
    # Regression: naive timestamps (no offset) crashed the verifier with a TypeError.
    def report(utc_suffix: str, alert: datetime) -> VerificationReport:
        run = Run(utc_suffix=utc_suffix)
        run.incident = run.incident.model_copy(update={"created_at": alert})
        ids = bad_checkout_release(run)
        deployment = run.registry.all()[0].data["deployments"][0]
        assert deployment["deployed_at"].endswith(f":00{utc_suffix}")  # really written that way
        return verify(run, hypothesis(DEPLOY, "checkout", *ids, version="v2.4.1"))

    explicit = report("+00:00", ALERT)
    assert explicit.status == "supported"
    for alert in (ALERT, ALERT.replace(tzinfo=None)):  # naive evidence, aware or naive alert
        assert report("", alert).model_dump() == explicit.model_dump()


def test_evidence_verifier_implements_the_verifier_interface() -> None:
    assert isinstance(EvidenceVerifier(), Verifier)
    # isinstance only checks that the methods exist, so compare their parameters too.
    for method in ("verify", "check_proposal"):
        ours = inspect.signature(getattr(EvidenceVerifier, method)).parameters
        interface = inspect.signature(getattr(Verifier, method)).parameters
        assert list(ours) == list(interface)
