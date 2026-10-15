"""Consolidation: turn one finished run into an ExperienceEpisode for memory.

Where it sits: the last step of a run. After the recovery oracle has judged the
mitigation, the controller hands the RunRecord here and adds the resulting episode to
the ExperienceStore (which applies the partition rules).

Every finished run is remembered: successes as reusable experience, everything else
as a cautionary case. The outcome is SUCCESS only when the independent recovery
oracle verified the recovery, so agent text can never make a run look successful.
The one exception is a held-out TEST scenario: it never becomes an episode, whatever
partition the run writes to, so no later run of it can find its own answer in memory.
Each episode carries provenance (simulated or not, environment, provider, seed,
config/fixture hashes, origin run and trace link), so it can always be traced back
to the run that produced it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from recap.core.ids import new_id, utcnow
from recap.core.models import (
    TERMINAL_STATES,
    ActionRecord,
    EpisodeOutcome,
    Evidence,
    EvidenceSummary,
    ExperienceEpisode,
    Hypothesis,
    Incident,
    IncidentState,
    MemoryPartition,
    RunRecord,
    ScenarioInfo,
)

SUMMARY_CHARS = 160

# Terminal states other than RESOLVED map one to one. RESOLVED is special: it
# becomes SUCCESS only with an oracle-verified recovery (see `_outcome`).
_OUTCOMES = {
    IncidentState.FAILED: EpisodeOutcome.FAILED,
    IncidentState.BLOCKED: EpisodeOutcome.BLOCKED,
    IncidentState.BUDGET_EXHAUSTED: EpisodeOutcome.BUDGET_EXHAUSTED,
}


class Consolidator:
    """Builds one ExperienceEpisode from a finished RunRecord (storing it is the caller's job)."""

    name = "consolidator-v1"

    def build_episode(
        self,
        *,
        record: RunRecord,
        evidence: Sequence[Evidence],
        scenario: ScenarioInfo,
        partition: MemoryPartition,
        simulated: bool,
        environment_kind: str,
    ) -> ExperienceEpisode:
        """Summarize a terminal run as an episode.

        Raises ValueError for an unusable run or a held-out test scenario.
        """
        incident = _check_inputs(record, scenario)
        verified = bool(record.recovery and record.recovery.verified)
        best = _supported_best(record)
        successful, unsuccessful = _split_actions(record.actions, verified)
        return ExperienceEpisode(
            episode_id=new_id("ep"),
            origin_run_id=record.run_id,
            scenario_id=scenario.id,
            failure_family=scenario.family,
            partition=partition,
            service=incident.service,
            symptoms=list(incident.symptoms),
            failure_mechanism=best.mechanism if best else None,
            mechanism_target=best.target_service if best else None,
            verified_evidence=_evidence_summaries(best, evidence),
            unsuccessful_actions=unsuccessful,
            successful_action=successful,
            outcome=_outcome(record.state, verified),
            verified=verified,
            verification_source=record.recovery.oracle_name if record.recovery else None,
            provenance=self._provenance(record, simulated, environment_kind),
            created_at=utcnow(),
        )

    def _provenance(
        self, record: RunRecord, simulated: bool, environment_kind: str
    ) -> dict[str, Any]:
        """Where the episode came from, so it can always be audited and reproduced."""
        return {
            "simulated": simulated,
            "environment": environment_kind,
            "provider": record.provider,
            "seed": record.versions.get("seed"),
            "config_hash": record.versions.get("config_hash"),
            "fixture_hash": record.versions.get("fixture_hash"),
            "origin_run_id": record.run_id,
            "created_by": self.name,
            "trace": f"runs/{record.run_id}/events",  # the API route serving the full trace
        }


def _check_inputs(record: RunRecord, scenario: ScenarioInfo) -> Incident:
    """Refuse runs that must not or cannot be remembered; return the run's incident."""
    if record.state not in TERMINAL_STATES:
        raise ValueError(f"run {record.run_id} is still {record.state}; only finished runs count")
    if record.incident is None:
        raise ValueError(f"run {record.run_id} has no incident to remember")
    if scenario.id != record.scenario_id:
        raise ValueError(f"scenario '{scenario.id}' is not this run's '{record.scenario_id}'")
    # Contamination guard for every target partition: even a DEMO episode (written when a
    # test scenario is run from the demo or the API) would hand later runs its answer.
    if scenario.partition == MemoryPartition.TEST:
        raise ValueError(
            f"episodes are never built from held-out test scenarios ('{scenario.id}' is one)"
        )
    return record.incident


def _outcome(state: IncidentState, verified: bool) -> EpisodeOutcome:
    """Map a terminal state to an outcome. RESOLVED counts as SUCCESS only if verified."""
    if state == IncidentState.RESOLVED:
        return EpisodeOutcome.SUCCESS if verified else EpisodeOutcome.FAILED
    return _OUTCOMES[state]


def _supported_best(record: RunRecord) -> Hypothesis | None:
    """The run's diagnosis, but only if the evidence verifier marked it 'supported'."""
    report = record.verification
    if report is None or report.status != "supported":
        return None
    return report.best


def _evidence_summaries(
    best: Hypothesis | None, evidence: Sequence[Evidence]
) -> list[EvidenceSummary]:
    """One-line summaries of the evidence behind the diagnosis (unknown IDs are skipped)."""
    if best is None:
        return []
    by_id = {item.id: item for item in evidence}
    return [
        EvidenceSummary(evidence_id=evidence_id, summary=_one_line(by_id[evidence_id].content))
        for evidence_id in dict.fromkeys(best.supporting_evidence_ids)  # unique, in order
        if evidence_id in by_id
    ]


def _one_line(text: str) -> str:
    """Collapse all whitespace to single spaces and keep the first 160 characters."""
    return " ".join(text.split())[:SUMMARY_CHARS]


def _split_actions(
    actions: Sequence[ActionRecord], verified: bool
) -> tuple[ActionRecord | None, list[ActionRecord]]:
    """Return (the action that fixed it, every other attempt).

    An action counts as the fix only if the oracle verified recovery; otherwise
    every attempt is kept as unsuccessful history.
    """
    recovered = [index for index, action in enumerate(actions) if action.result == "recovered"]
    if not verified or not recovered:
        return None, list(actions)
    last = recovered[-1]
    return actions[last], [action for index, action in enumerate(actions) if index != last]
