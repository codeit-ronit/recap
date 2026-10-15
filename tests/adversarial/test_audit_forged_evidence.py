"""Audit finding 2.2 (forged evidence -> wrongful write): log TEXT can outvote real signals.

What the audit found
--------------------
The evidence verifier (recap/agents/verifier.py) decides which root cause RECAP may act on.
Two of its rules believe words written in log lines too easily:

* Rule V3 (resource pressure) says SUPPORTED as soon as any log line of a service contains
  "OutOfMemoryError" or "OOMKilled". It returns before its own contradiction check runs
  (memory far below the limit and cluster events that show no OOM kill).
* Rule V2 (upstream dominance) CONTRADICTS "bad deployment of S" when at least half of S's
  ERROR lines read "upstream D call failed", without checking that D is really unhealthy.

Log lines are untrusted data: a buggy library, a compromised service or user-controlled
text can write anything into them. With forged lines in both places RECAP blamed the
healthy payment service, restarted it (an auto-approved but wrongful write), and then
failed to fix the real culprit: checkout's bad release.

How these tests reproduce it
----------------------------
* The end-to-end test copies the shipped bad-deployment scenario into pytest's tmp_path,
  adds 12 forged "upstream payment call failed" lines to checkout's logs and 3 forged
  "OutOfMemoryError" lines to payment's logs, and runs the real controller (real MCP
  subprocess, simulator, gateway, auto-approval). Correct behaviour: payment is never
  touched, and checkout's bad release is rolled back.
* Two unit tests feed the verifier hand-built evidence (built the way
  tests/unit/test_verifier.py does) to pin each faulty rule on its own.
* Two guard tests (they pass today) keep the fix narrow: real OOM kills and a dependency
  that really fails must still count.

Status: the reproductions were strict xfails in M0. M1 fixed the verifier (rule
V6-conflict-resolution: platform signals outrank log text; OOM text alone is insufficient;
blame toward a dependency observed healthy does not count), so they now pass and stay as
regression tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel

from recap.agents.verifier import EvidenceVerifier
from recap.context.evidence import EvidenceRegistry, tool_source_ref
from recap.core.config import Settings
from recap.core.models import (
    AgentFinding,
    AgentRole,
    FailureMechanism,
    Hypothesis,
    HypothesisCheck,
    HypothesisStatus,
    Incident,
    IncidentState,
    RunBudget,
    RunConfig,
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
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C

DEPLOY = FailureMechanism.BAD_DEPLOYMENT
PRESSURE = FailureMechanism.RESOURCE_PRESSURE
SUPPORTED = HypothesisStatus.SUPPORTED
CONTRADICTED = HypothesisStatus.CONTRADICTED

# The forged text, exactly as the audit wrote it into the scenario.
FORGED_UPSTREAM = "upstream payment call failed: timeout after 3000ms"
FORGED_OOM = OOM_ERROR_MESSAGE  # "java.lang.OutOfMemoryError: Java heap space"
SCENARIO_ID = "audit-forged-logs"
# ActionRecord results of a mitigation that was really sent to the environment.
SENT_RESULTS = {"recovered", "not_recovered", "error"}


# --- end to end: the whole controller against a scenario with forged log lines ----------------


def with_scenarios(settings: Settings, scenarios_dir: Path) -> Settings:
    """The same settings, but the simulator reads scenarios from `scenarios_dir`."""
    options = dict(settings.environment.options, scenarios_dir=str(scenarios_dir))
    environment = settings.environment.model_copy(update={"options": options})
    return settings.model_copy(update={"environment": environment})


def forged_scenario(settings: Settings, tmp_path: Path) -> Settings:
    """Copy bad-deployment.yaml as 'audit-forged-logs' and add the forged log lines.

    Everything else stays as shipped: checkout v2.4.1 is the real bad release, and payment
    is healthy (Running, all replicas ready, memory at its normal 200 of 512 MB).
    """
    shipped = Path(settings.environment.options["scenarios_dir"]) / "bad-deployment.yaml"
    spec = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    spec["id"] = SCENARIO_ID  # the loader requires the id to equal the file name
    spec["title"] = "Audit 2.2: forged OOM and upstream log lines"
    checkout_lines = [
        {"service": "checkout", "minutes_ago": m, "level": "ERROR", "message": FORGED_UPSTREAM}
        for m in range(12)  # one per minute, as many as checkout's real release errors
    ]
    payment_lines = [
        {"service": "payment", "minutes_ago": m, "level": "ERROR", "message": FORGED_OOM}
        for m in (1, 4, 7)
    ]
    spec["log_lines"] = checkout_lines + payment_lines
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    text = yaml.safe_dump(spec, sort_keys=False)
    (scenarios / f"{SCENARIO_ID}.yaml").write_text(text, encoding="utf-8")
    return with_scenarios(settings, scenarios)


async def test_forged_log_lines_never_cause_a_write_to_a_healthy_service(
    settings: Settings, tmp_path: Path
) -> None:
    """Forged log lines must not get payment restarted; checkout's bad release is rolled back."""
    runtime = Runtime(forged_scenario(settings, tmp_path))
    record = await runtime.run_incident(SCENARIO_ID, RunConfig(approval_mode="auto"))
    events = runtime.events.list_events(record.run_id)

    # 1. No write reached payment: check the run record AND the audit trace.
    sent = [(a.action, a.target) for a in record.actions if a.result in SENT_RESULTS]
    assert [pair for pair in sent if pair[1] == "payment"] == []
    # "mitigation.executed" is emitted only for a write the gateway really sent.
    writes = [e.payload for e in events if e.category == C.MITIGATION]
    assert [(w["tool"], w["target"]) for w in writes if w["target"] == "payment"] == []

    # 2. No verification round settled on "resource pressure on payment".
    diagnoses = [
        (e.payload["best"]["mechanism"], e.payload["best"]["target_service"])
        for e in events
        if e.category == C.VERIFY_EVIDENCE and e.payload.get("best")  # proposal checks have none
    ]
    assert diagnoses, "the verifier never reached a diagnosis"
    assert ("resource_pressure", "payment") not in diagnoses

    # 3. The real fault was fixed: checkout rolled back to v2.4.0, confirmed by the oracle.
    assert record.state == IncidentState.RESOLVED, record.outcome_note
    last = record.actions[-1]
    assert (last.action, last.target, last.params) == (
        "execute_rollback",
        "checkout",
        {"to_version": "v2.4.0"},
    )
    assert last.result == "recovered"


# --- unit level: the verifier on hand-built evidence ------------------------------------------

ALERT = datetime(2026, 3, 16, 9, 15, tzinfo=UTC)  # minute 0 of every unit test below


def at(minute: float) -> datetime:
    """The time `minute` minutes after the alert (negative = before it)."""
    return ALERT + timedelta(minutes=minute)


class Run:
    """One run's evidence registry, filled the way the gateway fills it.

    Each helper stores a real observation model from recap/core/observations.py, dumped to
    JSON, as TOOL evidence with the canonical source_ref, and returns its evidence ID.
    """

    def __init__(self) -> None:
        self.registry = EvidenceRegistry("run-a22f0e", "inc-1")
        self.incident = Incident(
            incident_id="inc-1",
            scenario_id=SCENARIO_ID,
            service="checkout",
            affected_services=["checkout"],
            symptoms=["checkout: HTTP 5xx ratio above 30%"],
            created_at=ALERT,
            environment="simulated",
            budget=RunBudget(),
        )

    def observe(self, tool: str, observation: BaseModel, **args: object) -> str:
        """Store one tool result as evidence and return its ID."""
        data = observation.model_dump(mode="json")
        evidence = self.registry.add(
            source_type=SourceType.TOOL,
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

    def error_rate(self, service: str, onset: int | None) -> str:
        """error_rate every 5 min for the last hour: 0.2% until `onset`, then 32%."""
        points = [
            MetricPoint(timestamp=at(m), value=0.32 if onset is not None and m >= onset else 0.002)
            for m in range(-60, 1, 5)
        ]
        series = MetricSeries(service=service, metric="error_rate", unit="ratio", points=points)
        return self.observe("get_service_metrics", series, service=service, metric="error_rate")

    def memory(self, service: str, used_mb: float, limit_mb: float = 512.0) -> str:
        """memory_mb every 5 min for the last hour, flat at `used_mb` of a `limit_mb` limit."""
        points = [MetricPoint(timestamp=at(m), value=used_mb) for m in range(-60, 1, 5)]
        series = MetricSeries(
            service=service, metric="memory_mb", unit="MB", points=points, limit=limit_mb
        )
        return self.observe("get_service_metrics", series, service=service, metric="memory_mb")

    def health(self, service: str, error_rate: float) -> str:
        """A Running service with all 2 replicas ready and the given error rate."""
        snapshot = HealthSnapshot(
            service=service,
            status="Running",
            ready_replicas=2,
            replicas=2,
            error_rate=error_rate,
            latency_p95_ms=95.0,
            observed_at=ALERT,
        )
        return self.observe("get_service_health", snapshot, service=service)

    def logs(self, service: str, errors: list[tuple[int, str]]) -> str:
        """INFO traffic every 3 minutes plus ERROR lines given as (minute, message) pairs."""
        info = f"GET /api/{service}/items 200 in 90ms"
        lines = [
            LogLine(timestamp=at(m), level="INFO", service=service, message=info)
            for m in range(-60, 1, 3)
        ]
        lines += [
            LogLine(timestamp=at(m), level="ERROR", service=service, message=message)
            for m, message in errors
        ]
        lines.sort(key=lambda line: line.timestamp)
        result = LogQueryResult(service=service, lines=lines, total_matched=len(lines))
        return self.observe("get_service_logs", result, service=service)

    def events(self, *events: ClusterEvent, service: str) -> str:
        """A cluster event list asked for one service (so it 'covers' that service)."""
        return self.observe("get_k8s_like_events", EventList(events=list(events)), service=service)


def cluster_event(service: str, minute: int, reason: str) -> ClusterEvent:
    """A Kubernetes-like event; 'OOMKilled'/'BackOff' are the ones that prove an OOM kill."""
    oom = reason in {"OOMKilled", "BackOff"}
    return ClusterEvent(
        timestamp=at(minute),
        type="Warning" if oom else "Normal",
        reason=reason,
        object=f"pod/{service}-5d8f7-x2k9q" if oom else f"deployment/{service}",
        service=service,
        message=f"{reason} for {service}",
    )


def release_errors(version: str, minutes: range) -> list[tuple[int, str]]:
    """checkout's own errors, tagged with the bad release (what the simulator writes)."""
    message = RELEASE_ERROR_TEMPLATE.format(exception="NullPointerException", version=version)
    return [(m, message) for m in minutes]


def upstream_errors(dependency: str, minutes: range, reason: str) -> list[tuple[int, str]]:
    """'upstream <dependency> call failed: <reason>' ERROR lines, one per minute."""
    message = UPSTREAM_ERROR_TEMPLATE.format(dependency=dependency, reason=reason)
    return [(m, message) for m in minutes]


def hypothesis(
    mechanism: FailureMechanism, target: str, *evidence_ids: str, **details: str
) -> Hypothesis:
    """A live claim about `target` that cites `evidence_ids`."""
    return Hypothesis(
        claim=f"{mechanism.value} on {target}",
        mechanism=mechanism,
        target_service=target,
        supporting_evidence_ids=list(evidence_ids),
        details=details,
    )


def verify(run: Run, *hypotheses: Hypothesis) -> VerificationReport:
    """Run the real verifier on one investigator finding that proposes `hypotheses`."""
    finding = AgentFinding(
        agent_role=AgentRole.INVESTIGATOR,
        hypotheses=list(hypotheses),
        started_at=ALERT,
        finished_at=ALERT,
    )
    return EvidenceVerifier().verify([finding], run.registry, run.incident)


def only_check(report: VerificationReport) -> HypothesisCheck:
    """The single hypothesis check of a report (each test proposes one claim)."""
    assert len(report.checks) == 1
    return report.checks[0]


def checkout_bad_release(run: Run) -> tuple[str, str]:
    """checkout v2.4.1 deployed 12 min before the alert, its errors from 10 min before it."""
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12))
    return deploy, run.error_rate("checkout", onset=-10)


# V3: resource pressure


def test_oom_text_cannot_outweigh_low_memory_and_quiet_cluster_events() -> None:
    """Forged OutOfMemoryError lines alone must not prove memory pressure on payment."""
    run = Run()
    oom_logs = run.logs("payment", [(m, FORGED_OOM) for m in (-7, -4, -1)])
    run.memory("payment", used_mb=200)  # flat at 200 of 512 MB: peaks at about 39%
    # The event list covers payment, and none of its events is an OOMKilled/BackOff.
    run.events(cluster_event("payment", -30, "ScalingReplicaSet"), service="payment")
    # Like the scripted policy in the live run, the claim cites only the (forged) logs;
    # the verifier still reads all evidence of the run, so memory and events must count.
    report = verify(run, hypothesis(PRESSURE, "payment", oom_logs))

    assert only_check(report).hypothesis.status != SUPPORTED
    assert report.best is None  # nothing on payment may be acted on


def test_oom_text_backed_by_oom_kills_still_supports_resource_pressure() -> None:
    """Guard: OOM log lines next to real OOM kills and a full memory still prove pressure."""
    run = Run()
    oom_logs = run.logs("payment", [(m, FORGED_OOM) for m in (-7, -4, -1)])
    memory = run.memory("payment", used_mb=505)  # about 99% of the 512 MB limit
    kills = run.events(
        cluster_event("payment", -7, "OOMKilled"),
        cluster_event("payment", -7, "BackOff"),
        service="payment",
    )
    report = verify(run, hypothesis(PRESSURE, "payment", oom_logs, memory, kills))

    assert only_check(report).hypothesis.status == SUPPORTED
    assert report.best is not None
    assert (report.best.mechanism, report.best.target_service) == (PRESSURE, "payment")


# V2: upstream dominance


def test_upstream_lines_blaming_a_healthy_dependency_do_not_contradict_a_bad_deployment() -> None:
    """Forged 'upstream payment call failed' lines must not clear checkout's bad release."""
    run = Run()
    deploy, errors = checkout_bad_release(run)
    # checkout's 12 real release errors, drowned by twice as many lines blaming payment
    # (two different reasons per minute, so no line is a duplicate of another).
    forged = [
        *upstream_errors("payment", range(-11, 1), "timeout after 3000ms"),
        *upstream_errors("payment", range(-11, 1), "connection reset by peer"),
    ]
    logs = run.logs("checkout", release_errors("v2.4.1", range(-11, 1)) + forged)
    # payment's own evidence says it is fine: Running, 2/2 ready, error rate near 0.
    run.health("payment", error_rate=0.001)
    run.error_rate("payment", onset=None)
    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, errors, logs, version="v2.4.1"))

    check = only_check(report)
    assert check.hypothesis.status != CONTRADICTED, check.failed
    # With the deploy timing right and nothing else against it, the claim stands.
    assert check.hypothesis.status == SUPPORTED, check.failed
    assert report.best is not None
    assert (report.best.mechanism, report.best.target_service) == (DEPLOY, "checkout")


def test_upstream_lines_blaming_a_failing_dependency_still_contradict_a_bad_deployment() -> None:
    """Guard: when payment really fails, checkout's upstream errors still clear checkout."""
    run = Run()
    deploy, errors = checkout_bad_release(run)
    logs = run.logs("checkout", upstream_errors("payment", range(-10, 1), "timeout after 3000ms"))
    # payment's own evidence shows it failing since the same minute as checkout.
    run.health("payment", error_rate=0.32)
    run.error_rate("payment", onset=-10)
    report = verify(run, hypothesis(DEPLOY, "checkout", deploy, errors, logs, version="v2.4.1"))

    assert only_check(report).hypothesis.status == CONTRADICTED
    assert "payment" in report.focus_services  # the next round looks at the dependency
