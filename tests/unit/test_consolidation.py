"""Tests for the Consolidator: how a finished run becomes an experience episode.

The key rules: an episode is a SUCCESS only when the independent recovery oracle
verified recovery, and everything else is remembered as a cautionary case. A held-out
test scenario never becomes an episode at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from recap.agents.verifier import EvidenceVerifier
from recap.core.models import (
    TERMINAL_STATES,
    ActionRecord,
    AgentFinding,
    AgentRole,
    EpisodeOutcome,
    Evidence,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    HypothesisCheck,
    HypothesisStatus,
    Incident,
    IncidentState,
    MemoryPartition,
    ProviderInfo,
    RecoveryResult,
    RunBudget,
    RunConfig,
    RunRecord,
    ScenarioInfo,
    SourceType,
    VerificationReport,
)
from recap.memory.consolidation import Consolidator
from recap.memory.store import ExperienceStore, MemoryAccess
from tests.unit.test_verifier import DEPLOY, Run, hypothesis, release_errors

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
SYMPTOMS = ["checkout: HTTP 5xx ratio above 30% (SLO 1%)"]
SCENARIO = ScenarioInfo(
    id="bad-deployment",
    title="Checkout errors right after a new release",
    family="bad_deployment",
    partition=MemoryPartition.DEMO,
    alert_service="checkout",
)
BEST = Hypothesis(
    claim="checkout v2.4.1 release causes the errors",
    mechanism=FailureMechanism.BAD_DEPLOYMENT,
    target_service="checkout",
    supporting_evidence_ids=["ev-1", "ev-2", "ev-1", "ev-from-another-run"],
    status=HypothesisStatus.SUPPORTED,
)
RESTART_FAILED = ActionRecord(
    action="restart_service", target="checkout", result="not_recovered", at=T0
)
ROLLBACK_WORKED = ActionRecord(
    action="execute_rollback",
    target="checkout",
    params={"to_version": "v2.4.0"},
    result="recovered",
    at=T0,
)


def make_evidence(evidence_id: str, content: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        incident_id="inc-1",
        source_type=SourceType.TOOL,
        source_ref="mcp:get_service_logs(service=checkout)",
        timestamp=T0,
        content=content,
        tool_name="get_service_logs",
    )


EVIDENCE = [
    make_evidence("ev-1", "ERROR NullPointerException\n   in PricingClient " + "x" * 300),
    make_evidence("ev-2", "deployment checkout v2.4.1 at 09:48"),
    make_evidence("ev-3", "not cited by the diagnosis"),
]


def make_record(
    state: IncidentState = IncidentState.RESOLVED,
    *,
    verified: bool | None = True,  # None = the oracle never ran
    status: Literal["supported", "insufficient"] = "supported",
) -> RunRecord:
    """A bad-deployment run that ended in `state` after a failed restart and a rollback."""
    incident = Incident(
        incident_id="inc-1",
        scenario_id="bad-deployment",
        service="checkout",
        affected_services=["checkout"],
        symptoms=SYMPTOMS,
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )
    recovery = None
    if verified is not None:
        recovery = RecoveryResult(
            healthy_before=False,
            healthy_after=verified,
            oracle_name="environment-health-oracle",
            verified=verified,
            checked_at=T0,
        )
    return RunRecord(
        run_id="run-abc123",
        scenario_id="bad-deployment",
        config=RunConfig(),
        incident=incident,
        state=state,
        provider="deterministic-scripted-policy",
        verification=VerificationReport(status=status, best=BEST),
        actions=[RESTART_FAILED, ROLLBACK_WORKED],
        recovery=recovery,
        versions={"seed": 11, "config_hash": "cfg-hash", "fixture_hash": "fix-hash"},
    )


def finding(role: AgentRole, *hypotheses: Hypothesis) -> AgentFinding:
    return AgentFinding(agent_role=role, hypotheses=list(hypotheses), started_at=T0, finished_at=T0)


def consolidate(
    record: RunRecord,
    scenario: ScenarioInfo = SCENARIO,
    partition: MemoryPartition = MemoryPartition.DEMO,
) -> ExperienceEpisode:
    """Consolidate `record` as a simulated run (the common case in these tests)."""
    return Consolidator().build_episode(
        record=record,
        evidence=EVIDENCE,
        scenario=scenario,
        partition=partition,
        simulated=True,
        environment_kind="simulated",
    )


# --- outcome --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "verified", "expected"),
    [
        (IncidentState.RESOLVED, True, EpisodeOutcome.SUCCESS),
        (IncidentState.RESOLVED, False, EpisodeOutcome.FAILED),  # oracle says not recovered
        (IncidentState.RESOLVED, None, EpisodeOutcome.FAILED),  # oracle never ran
        (IncidentState.FAILED, False, EpisodeOutcome.FAILED),
        (IncidentState.BLOCKED, None, EpisodeOutcome.BLOCKED),
        (IncidentState.BUDGET_EXHAUSTED, None, EpisodeOutcome.BUDGET_EXHAUSTED),
    ],
)
def test_outcome_follows_terminal_state_and_oracle(
    state: IncidentState, verified: bool | None, expected: EpisodeOutcome
) -> None:
    episode = consolidate(make_record(state, verified=verified))
    assert episode.outcome == expected
    assert episode.verified is (verified is True)


@pytest.mark.parametrize("state", sorted(set(IncidentState) - TERMINAL_STATES))
def test_unfinished_runs_are_refused(state: IncidentState) -> None:
    with pytest.raises(ValueError, match="only finished runs"):
        consolidate(make_record(state))


# --- actions --------------------------------------------------------------------------


def test_verified_run_records_the_fix_and_the_failed_attempts() -> None:
    episode = consolidate(make_record(verified=True))
    assert episode.successful_action == ROLLBACK_WORKED
    assert episode.unsuccessful_actions == [RESTART_FAILED]


def test_unverified_run_has_no_successful_action() -> None:
    # The rollback *claims* "recovered", but the oracle did not verify it.
    episode = consolidate(make_record(verified=False))
    assert episode.successful_action is None
    assert episode.unsuccessful_actions == [RESTART_FAILED, ROLLBACK_WORKED]


# --- diagnosis and evidence -----------------------------------------------------------


def test_supported_diagnosis_and_its_evidence_are_kept() -> None:
    episode = consolidate(make_record())
    assert episode.failure_mechanism == FailureMechanism.BAD_DEPLOYMENT
    assert episode.mechanism_target == "checkout"
    # Cited IDs only (unique, in order); an ID this run never recorded is skipped.
    assert [item.evidence_id for item in episode.verified_evidence] == ["ev-1", "ev-2"]
    first = episode.verified_evidence[0].summary
    assert first.startswith("ERROR NullPointerException in PricingClient")
    assert "\n" not in first and len(first) == 160


def test_unsupported_diagnosis_is_not_remembered() -> None:
    # The report still carries a "best" guess, but the verifier did not support it.
    episode = consolidate(make_record(IncidentState.FAILED, verified=None, status="insufficient"))
    assert episode.failure_mechanism is None
    assert episode.mechanism_target is None
    assert episode.verified_evidence == []


# --- labels and provenance -----------------------------------------------------------


def test_episode_carries_incident_labels_and_provenance() -> None:
    # A non-default environment kind shows the label is passed in, never hardcoded,
    # so swapping the environment adapter needs no change here.
    episode = Consolidator().build_episode(
        record=make_record(),
        evidence=EVIDENCE,
        scenario=SCENARIO,
        partition=MemoryPartition.DEMO,
        simulated=True,
        environment_kind="fake",
    )
    assert episode.episode_id.startswith("ep-")
    assert (episode.service, episode.symptoms) == ("checkout", SYMPTOMS)
    assert (episode.scenario_id, episode.failure_family) == ("bad-deployment", "bad_deployment")
    assert episode.origin_run_id == "run-abc123"
    assert episode.partition == MemoryPartition.DEMO
    assert episode.verification_source == "environment-health-oracle"
    assert episode.provenance == {
        "simulated": True,
        "environment": "fake",
        "provider": "deterministic-scripted-policy",
        # This record names no provider info or retrieval mode (a scripted run would).
        "origin": None,
        "model": None,
        "prompt_version": None,
        "weights": None,
        "retrieval": None,
        "code_version": None,
        "seed": 11,
        "config_hash": "cfg-hash",
        "fixture_hash": "fix-hash",
        "origin_run_id": "run-abc123",
        "created_by": "consolidator-v2",
        "trace": "runs/run-abc123/events",
    }


def test_a_v2_episode_summarizes_the_trajectory_by_deterministic_extraction() -> None:
    # The diagnosis is the hypothesis of the check that supported it, as in a real report.
    diagnosis = BEST.model_copy(update={"details": {"version": "v2.4.1"}})
    record = make_record().model_copy(
        update={
            "provider_info": ProviderInfo(
                name="ollama:qwen3:8b",
                kind="llm",
                origin="local_llm",
                model="qwen3:8b",
                prompt_version="prompts-v4.4",
                server={"weights": "sha256:" + "a" * 64},
            ),
            "verification": VerificationReport(
                status="supported",
                best=diagnosis,
                checks=[
                    HypothesisCheck(
                        hypothesis=diagnosis,
                        passed=["errors on checkout began 2 min after v2.4.1 was deployed"],
                        rule_ids=["V1-structural", "V2-deploy-timing"],
                        proposers=[AgentRole.INFRASTRUCTURE],
                    )
                ],
            ),
        }
    )
    episode = consolidate(record)

    assert episode.summary_method == "deterministic-extraction"
    assert [step.call for step in episode.trajectory] == [e.source_ref for e in EVIDENCE]
    assert [c.proposers for c in episode.checked_claims] == [[AgentRole.INFRASTRUCTURE]]
    assert episode.diagnosis_basis == ["errors on checkout began 2 min after v2.4.1 was deployed"]
    assert episode.fix_preconditions == {
        "action": "execute_rollback",
        "target": "checkout",
        "mechanism": "bad_deployment",
        "bad_version": "v2.4.1",
        "to_version": "v2.4.0",
    }
    model = {k: episode.provenance[k] for k in ("origin", "model", "prompt_version", "weights")}
    assert model == {
        "origin": "local_llm",
        "model": "qwen3:8b",
        "prompt_version": "prompts-v4.4",
        "weights": "sha256:" + "a" * 64,
    }
    # What a provider sees: the checks the past run made, never the labels.
    view = episode.agent_view()
    assert view.checks == [e.source_ref for e in EVIDENCE]
    assert not hasattr(view, "provenance") and not hasattr(view, "scenario_id")


def test_the_diagnosis_basis_is_the_supported_releases_own_lines_in_any_order() -> None:
    """Regression: two bad-release claims on checkout are judged apart by their version (a
    detail_conflict). The basis was the first check with the same mechanism and service, so
    with the contradicted v2.4.0 claim first, its lines were stored as the reason for v2.4.1."""
    run = Run()
    deploy = run.deployments(("checkout", "v2.4.0", -2880), ("checkout", "v2.4.1", -12))
    rate = run.error_rate("checkout", onset=-10)
    logs = run.logs("checkout", release_errors("v2.4.1", range(-11, 1)))
    older = finding(
        AgentRole.INVESTIGATOR, hypothesis(DEPLOY, "checkout", deploy, rate, version="v2.4.0")
    )
    newer = finding(
        AgentRole.INFRASTRUCTURE, hypothesis(DEPLOY, "checkout", deploy, logs, version="v2.4.1")
    )

    for order in ([older, newer], [newer, older]):
        report = EvidenceVerifier().verify(order, run.registry, run.incident)
        statuses = {c.hypothesis.details["version"]: c for c in report.checks}
        rejected, accepted = statuses["v2.4.0"], statuses["v2.4.1"]
        assert rejected.hypothesis.status == HypothesisStatus.CONTRADICTED
        assert accepted.hypothesis.status == HypothesisStatus.SUPPORTED
        assert rejected.passed != accepted.passed

        episode = consolidate(make_record().model_copy(update={"verification": report}))
        assert episode.diagnosis_basis == accepted.passed
        # The two claims can be told apart in the episode, as the verifier told them apart.
        claims = {c.detail: c.status for c in episode.checked_claims}
        assert claims == {
            "v2.4.0": HypothesisStatus.CONTRADICTED,
            "v2.4.1": HypothesisStatus.SUPPORTED,
        }


def test_the_basis_is_the_claim_with_the_diagnosis_own_detail() -> None:
    """Mechanism, service and origin alone do not identify the diagnosis' check: even if two
    claims that differ only in their detail were both supported, the basis is the one whose
    detail the diagnosis has. The verifier records each check's detail, resolved from the
    evidence when the claim names none (here: the newest release, v2.4.1)."""
    named = BEST.model_copy(update={"details": {"version": "v2.4.0"}})
    checks = [
        HypothesisCheck(hypothesis=named, passed=["v2.4.0 was deployed then"], detail="v2.4.0"),
        HypothesisCheck(hypothesis=BEST, passed=["v2.4.1 was deployed then"], detail="v2.4.1"),
    ]
    report = VerificationReport(status="supported", best=BEST, checks=checks)
    episode = consolidate(make_record().model_copy(update={"verification": report}))

    assert episode.diagnosis_basis == ["v2.4.1 was deployed then"]
    assert [c.detail for c in episode.checked_claims] == ["v2.4.0", "v2.4.1"]


def test_run_without_oracle_has_no_verification_source() -> None:
    episode = consolidate(make_record(IncidentState.BLOCKED, verified=None))
    assert episode.verification_source is None


# --- refusals -------------------------------------------------------------------------


def test_scenario_must_match_the_run() -> None:
    other = SCENARIO.model_copy(update={"id": "other-scenario"})
    with pytest.raises(ValueError, match="is not this run's"):
        consolidate(make_record(), other)


def test_run_without_incident_is_refused() -> None:
    with pytest.raises(ValueError, match="no incident"):
        consolidate(make_record().model_copy(update={"incident": None}))


@pytest.mark.parametrize("partition", list(MemoryPartition))
def test_held_out_test_scenarios_never_become_memory(partition: MemoryPartition) -> None:
    # Regression: running a test scenario from the demo or the API used to write a DEMO
    # episode, which later runs of that same scenario could then reuse as a shortcut.
    held_out = SCENARIO.model_copy(update={"partition": MemoryPartition.TEST})
    with pytest.raises(ValueError, match="never built from held-out test scenarios"):
        consolidate(make_record(), held_out, partition=partition)


# --- end to end with the store ---------------------------------------------------------


def test_consolidated_episodes_round_trip_through_the_store(tmp_path: Path) -> None:
    store = ExperienceStore(tmp_path / "memory.db")
    success = consolidate(make_record(verified=True))
    caution = consolidate(make_record(IncidentState.FAILED, verified=False))

    assert store.add(success, MemoryAccess.default()) is True
    assert store.add(caution, MemoryAccess.default()) is True
    assert store.get(success.episode_id) == success
    assert {e.outcome for e in store.list()} == {EpisodeOutcome.SUCCESS, EpisodeOutcome.FAILED}
