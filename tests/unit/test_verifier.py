"""Tests for the EvidenceVerifier: rules V1-V6, merging, the best choice and proposal checks.

Evidence is built the way the gateway records it: a real observation model from
recap/core/observations.py, dumped with model_dump(mode="json"), stored in an EvidenceRegistry.
Minute 0 is the alert time.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from recap.agents.merger import V7
from recap.agents.verifier import (
    PROPOSAL_RULES,
    V1,
    V1_GROUNDING,
    V2_TIMING,
    V2_UPSTREAM,
    V3,
    V4,
    V5,
    V6,
    EvidenceVerifier,
)
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
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from recap.core.signals import OOM_ERROR_MESSAGE, RELEASE_ERROR_TEMPLATE, UPSTREAM_ERROR_TEMPLATE

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

    def health(
        self, service: str, error_rate: float, status: str = "Running", ready: int = 2
    ) -> str:
        """One health snapshot at the alert time (2 replicas)."""
        snapshot = HealthSnapshot(
            service=service,
            status=status,  # type: ignore[arg-type]
            ready_replicas=ready,
            replicas=2,
            error_rate=error_rate,
            latency_p95_ms=95.0,
            observed_at=at(0),
        )
        return self.observe("get_service_health", snapshot, service=service)


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
    assert check.rule_ids == [
        "V1-structural",
        "V2-deploy-timing",
        "V2-upstream-dominance",
        "V1-grounding",
    ]
    assert check.basis_evidence_ids == ids  # deployment, error rate, and checkout's own logs
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


# --- V1-grounding: any listing of what the rules relied on counts -------------------------------


def again(run: Run, evidence_id: str, model: type[BaseModel], **args: object) -> str:
    """Record the observation of `evidence_id` once more, as a call with other arguments.

    Real runs list the same things twice (deployments by limit and by service, events for
    the namespace and for one service), and every new epoch repeats its calls.
    """
    item = run.registry.get(evidence_id)
    assert item is not None and item.tool_name
    return run.observe(item.tool_name, model.model_validate(item.data), **args)


def test_a_claim_citing_either_listing_of_its_release_is_grounded() -> None:
    run = Run()
    first, _, _ = bad_checkout_release(run)
    second = again(run, first, DeploymentList, service="checkout")

    for cited in (second, first):
        check = only_check(verify(run, hypothesis(DEPLOY, "checkout", cited, version="v2.4.1")))
        # Before, the basis named only the listing recorded first, so a claim citing the
        # second one (the listing its agent had just made) stayed insufficient.
        assert check.hypothesis.status == SUPPORTED
        assert {first, second} <= set(check.basis_evidence_ids)


def test_a_claim_citing_either_event_list_of_its_oom_kill_is_grounded() -> None:
    for per_service_first in (True, False):
        run = Run()
        kill = oom_event("inventory", -20)
        if per_service_first:
            per_service, namespace = run.events(kill, service="inventory"), run.events(kill)
        else:
            namespace, per_service = run.events(kill), run.events(kill, service="inventory")

        for cited in (per_service, namespace):
            check = only_check(verify(run, hypothesis(PRESSURE, "inventory", cited)))
            assert check.hypothesis.status == SUPPORTED  # whichever list was recorded first
            assert "1 OOMKilled/BackOff events for inventory" in check.passed  # counted once
            assert {per_service, namespace} <= set(check.basis_evidence_ids)


def test_a_claim_citing_either_log_query_of_the_same_lines_is_grounded() -> None:
    run = Run()
    first = run.logs("cart", upstream_errors("inventory", range(-10, 1)))
    second = again(run, first, LogQueryResult, service="cart", level="ERROR")

    for cited in (first, second):
        claim = hypothesis(DEPENDENCY, "cart", cited, dependency="inventory")
        check = only_check(verify(run, claim))
        assert check.hypothesis.status == SUPPORTED
        assert "11 ERROR lines of cart say 'upstream inventory call failed'" in check.passed

    # The same for a release whose errors are read from its log lines (no error_rate metric).
    release = Run()
    deploy = release.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12))
    logs = release.logs("checkout", release_errors("v2.4.1", range(-11, 1)))
    repeated = again(release, logs, LogQueryResult, service="checkout", level="ERROR")
    for cited in (logs, repeated):
        check = only_check(verify(release, hypothesis(DEPLOY, "checkout", cited)))
        assert check.hypothesis.status == SUPPORTED
        assert {deploy, logs, repeated} <= set(check.basis_evidence_ids)


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
        "VP4-restore-point",
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


def test_the_agents_readme_names_every_rule_and_every_file() -> None:
    # Before, it still described the pre-M3 verifier: no V1-grounding, V7 or VP4, and no
    # merger.py (whose rules replaced the "more evidence wins a tie" ranking it described).
    package = Path(inspect.getfile(EvidenceVerifier)).parent
    readme = (package / "README.md").read_text(encoding="utf-8")
    rules = [V1, V1_GROUNDING, V2_TIMING, V2_UPSTREAM, V3, V4, V5, V6, V7, *PROPOSAL_RULES]
    files = sorted(path.name for path in package.glob("*.py") if path.name != "__init__.py")
    assert [rule for rule in rules if f"`{rule}`" not in readme] == []
    assert [name for name in files if f"`{name}`" not in readme] == []


# --- V6: platform signals outrank log text (audit finding 2.2) -------------------------------


def oom_lines(minutes: range) -> dict[int, str]:
    return dict.fromkeys(minutes, OOM_ERROR_MESSAGE)


def test_oom_log_text_alone_leaves_resource_pressure_insufficient() -> None:
    """Text can be forged: without a memory metric or OOM kills it proves nothing."""
    run = Run()
    logs = run.logs("payment", oom_lines(range(-7, 0, 3)))

    check = only_check(verify(run, hypothesis(PRESSURE, "payment", logs)))

    assert check.hypothesis.status == INSUFFICIENT
    assert any("only log text claims out-of-memory on payment" in m for m in check.failed)


def test_oom_log_text_against_a_healthy_service_is_contradicted() -> None:
    run = Run()
    logs = run.logs("payment", oom_lines(range(-7, 0, 3)))
    health = run.health("payment", error_rate=0.001)

    check = only_check(verify(run, hypothesis(PRESSURE, "payment", logs)))

    assert check.hypothesis.status == CONTRADICTED
    assert health in check.hypothesis.contradicting_evidence_ids
    assert "V6-conflict-resolution" in check.rule_ids
    assert any("the platform signals win" in m for m in check.failed)


def test_oom_text_against_low_memory_and_quiet_events_names_both_sides() -> None:
    run = Run()
    logs = run.logs("payment", oom_lines(range(-7, 0, 3)))
    memory = run.memory("payment", ratio=0.39)
    events = run.events(scaling_event("payment", -30), service="payment")

    check = only_check(verify(run, hypothesis(PRESSURE, "payment", logs)))

    assert check.hypothesis.status == CONTRADICTED
    assert {memory, events} <= set(check.hypothesis.contradicting_evidence_ids)
    conflict = next(m for m in check.failed if "conflicts with platform signals" in m)
    assert logs in conflict and memory in conflict and events in conflict


def test_a_dependency_failure_on_a_healthy_dependency_is_contradicted() -> None:
    run = Run()
    logs = run.logs("checkout", upstream_errors("payment", range(-6, 1)))
    health = run.health("payment", error_rate=0.001)
    rate = run.error_rate("payment", onset=None)

    check = only_check(verify(run, hypothesis(DEPENDENCY, "checkout", logs, dependency="payment")))

    assert check.hypothesis.status == CONTRADICTED
    assert {health, rate} <= set(check.hypothesis.contradicting_evidence_ids)
    assert "V6-conflict-resolution" in check.rule_ids


def test_one_unhealthy_observation_means_the_dependency_is_not_healthy() -> None:
    """A Running snapshot does not outweigh an error rate that went above the threshold."""
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12))
    rate = run.error_rate("checkout", onset=-10)
    logs = run.logs("checkout", upstream_errors("payment", range(-10, 1)))  # all its errors
    run.health("payment", error_rate=0.001)  # a snapshot that looks fine...
    run.error_rate("payment", onset=-10)  # ...but payment really failed during the incident

    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, rate, logs, version="v2.4.1"))

    assert only_check(report).hypothesis.status == CONTRADICTED  # checkout is a victim
    assert "payment" in report.focus_services


def test_a_healthy_dependency_does_not_get_the_deepest_root_cause_bonus() -> None:
    """Blame toward a healthy service must not make it outrank the real bad release."""
    run = Run()
    deploy, rate, release_logs = bad_checkout_release(run)
    # A few forged lines blame payment (a minority, so checkout's own claim still stands).
    blame = run.logs("checkout", upstream_errors("payment", range(-3, 1)))
    # payment had an old OOM kill but is healthy now and through the whole incident.
    old_kill = run.events(oom_event("payment", -55), service="payment")
    run.health("payment", error_rate=0.001)
    run.error_rate("payment", onset=None)

    report = verify(
        run,
        hypothesis(DEPLOY, "checkout", deploy, rate, release_logs, blame, version="v2.4.1"),
        hypothesis(PRESSURE, "payment", old_kill),
    )

    statuses = {c.hypothesis.target_service: c.hypothesis.status for c in report.checks}
    # Before M3 payment's claim counted as supported (the kill did happen) and checkout won
    # only because it cited more evidence. Now V6 also covers the target: payment is
    # observed healthy, so there is nothing on it to fix.
    assert statuses == {"checkout": SUPPORTED, "payment": CONTRADICTED}
    payment = next(c for c in report.checks if c.hypothesis.target_service == "payment")
    assert any("payment is observed healthy" in line for line in payment.failed)
    assert report.best is not None and report.best.target_service == "checkout"
    assert not [d for d in report.disagreements if d.kind == "competing_root_causes"]


# --- epochs: after a fix, the fixed service's older state no longer counts (audit 2.6) -------


def test_oom_kills_from_before_a_restart_no_longer_support_resource_pressure() -> None:
    run = Run()
    old_kills = run.events(oom_event("payment", -6), oom_event("payment", -4), service="payment")
    restarted_at = at(1)

    before = verify(run, hypothesis(PRESSURE, "payment", old_kills))
    after = EvidenceVerifier().verify(
        [finding(hypothesis(PRESSURE, "payment", old_kills))],
        run.registry,
        run.incident,
        remediated={"payment": restarted_at},
    )

    assert only_check(before).hypothesis.status == SUPPORTED
    assert only_check(after).hypothesis.status != SUPPORTED  # the restart ended those kills


def test_oom_kills_after_the_fix_still_count() -> None:
    run = Run()
    kills = run.events(oom_event("payment", -2), service="payment")

    report = EvidenceVerifier().verify(
        [finding(hypothesis(PRESSURE, "payment", kills))],
        run.registry,
        run.incident,
        remediated={"payment": at(-5)},  # the fix came before these kills: they are fresh
    )

    assert only_check(report).hypothesis.status == SUPPORTED


def checkout_blamed_payment_before_payment_was_fixed(run: Run) -> Hypothesis:
    """checkout v2.4.1 is bad; most of its errors blamed payment until payment's fix at -9."""
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -30))
    rate = run.error_rate("checkout", onset=-28)
    errors = {
        **release_errors("v2.4.1", range(-8, 1)),
        **upstream_errors("payment", range(-28, -8)),
    }
    logs = run.logs("checkout", errors)
    return hypothesis(DEPLOY, "checkout", deploy, rate, logs, version="v2.4.1")


def test_old_blame_stops_counting_once_the_fixed_dependency_is_seen_healthy() -> None:
    """A fix on payment plus fresh evidence that payment is healthy: the blame was stale."""
    run = Run()
    claim = checkout_blamed_payment_before_payment_was_fixed(run)
    run.health("payment", error_rate=0.001)  # observed after the fix (minute 0 > -9)
    run.error_rate("payment", onset=None)

    report = EvidenceVerifier().verify(
        [finding(claim)], run.registry, run.incident, remediated={"payment": at(-9)}
    )

    assert only_check(report).hypothesis.status == SUPPORTED
    assert any("payment is observed healthy" in m for m in only_check(report).passed)


def test_a_fix_on_a_dependency_alone_does_not_clear_the_blame() -> None:
    """A new epoch starts only when the fix did NOT recover the system: without fresh evidence
    that payment is healthy, its blame still counts, so the innocent caller is not picked."""
    run = Run()
    claim = checkout_blamed_payment_before_payment_was_fixed(run)

    report = EvidenceVerifier().verify(
        [finding(claim)], run.registry, run.incident, remediated={"payment": at(-9)}
    )

    assert only_check(report).hypothesis.status == CONTRADICTED  # most errors blame payment
    assert "payment" in report.focus_services


def test_a_slow_dependency_is_not_healthy_even_with_few_errors() -> None:
    """A dependency whose requests time out (p95 far above the SLO) keeps its blame."""
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12))
    rate = run.error_rate("checkout", onset=-10)
    logs = run.logs("checkout", upstream_errors("payment", range(-8, 1)))
    slow = HealthSnapshot(
        service="payment",
        status="Running",
        ready_replicas=2,
        replicas=2,
        error_rate=0.01,
        latency_p95_ms=9500.0,
        observed_at=at(0),
    )
    run.observe("get_service_health", slow, service="payment")

    check = only_check(
        verify(run, hypothesis(DEPLOY, "checkout", deploy, rate, logs, version="v2.4.1"))
    )

    assert check.hypothesis.status == CONTRADICTED  # its errors come from payment
    assert not any("observed healthy" in m for m in check.passed)


def test_after_a_fix_with_no_new_log_lines_the_errors_cannot_be_attributed() -> None:
    run = Run()
    deploy, rate, logs = bad_checkout_release(run)
    claim = hypothesis(DEPLOY, "checkout", deploy, rate, logs, version="v2.4.1")

    report = EvidenceVerifier().verify(
        [finding(claim)], run.registry, run.incident, remediated={"checkout": at(1)}
    )

    check = only_check(report)
    assert check.hypothesis.status != SUPPORTED
    assert any("no log lines of checkout since its last fix" in m for m in check.failed)


def test_an_event_list_from_before_a_fix_cannot_prove_there_were_no_oom_kills() -> None:
    run = Run()
    old_kills = run.events(oom_event("payment", -6), service="payment")
    memory = run.memory("payment", ratio=0.39)

    report = EvidenceVerifier().verify(
        [finding(hypothesis(PRESSURE, "payment", old_kills, memory))],
        run.registry,
        run.incident,
        remediated={"payment": at(-5)},
    )

    check = only_check(report)
    assert check.hypothesis.status == INSUFFICIENT  # not contradicted by a stale list
    assert any("no cluster events since its last fix" in m for m in check.failed)


def test_a_release_that_was_already_replaced_cannot_explain_the_errors() -> None:
    run = Run()
    deploy = run.deployments(
        ("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12), ("checkout", "v2.4.0", 1)
    )
    rate = run.error_rate("checkout", onset=-10)

    check = only_check(verify(run, hypothesis(DEPLOY, "checkout", deploy, rate, version="v2.4.1")))

    assert check.hypothesis.status == CONTRADICTED
    assert any("checkout no longer runs v2.4.1" in message for message in check.failed)


def test_an_unhealthy_snapshot_alone_means_the_dependency_is_not_healthy() -> None:
    """payment is visibly down (CrashLoopBackOff, 0/2 ready, no error-rate series): blame
    toward it counts, so checkout's claim is not wrongly cleared."""
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12))
    rate = run.error_rate("checkout", onset=-10)
    logs = run.logs("checkout", upstream_errors("payment", range(-10, 1)))  # all errors blame it
    run.health("payment", error_rate=0.32, status="CrashLoopBackOff", ready=0)

    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, rate, logs, version="v2.4.1"))

    assert only_check(report).hypothesis.status == CONTRADICTED  # checkout is a victim
    assert "payment" in report.focus_services


def test_memory_near_the_limit_before_a_restart_no_longer_supports_pressure() -> None:
    """A restart resets memory: the pre-fix peak must not re-support the old diagnosis."""
    run = Run()
    memory = run.memory("payment", ratio=0.99)  # 99% of the limit, minutes -60..0
    claim = hypothesis(PRESSURE, "payment", memory)

    before = verify(run, claim)
    after = EvidenceVerifier().verify(
        [finding(claim)], run.registry, run.incident, remediated={"payment": at(1)}
    )

    assert only_check(before).hypothesis.status == SUPPORTED
    assert only_check(after).hypothesis.status != SUPPORTED


def test_log_lines_from_before_a_fix_no_longer_support_a_dependency_failure() -> None:
    """checkout was fixed at +1: its older 'upstream payment call failed' lines are stale."""
    run = Run()
    logs = run.logs("checkout", upstream_errors("payment", range(-6, 1)))
    claim = hypothesis(DEPENDENCY, "checkout", logs, dependency="payment")

    before = verify(run, claim)
    after = EvidenceVerifier().verify(
        [finding(claim)], run.registry, run.incident, remediated={"checkout": at(1)}
    )

    assert only_check(before).hypothesis.status == SUPPORTED
    assert only_check(after).hypothesis.status == INSUFFICIENT  # stale, not contradicting
    assert any("no log lines of checkout since its last fix" in m for m in only_check(after).failed)
