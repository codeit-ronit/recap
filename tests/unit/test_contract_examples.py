"""Golden examples keep the serialized contracts stable.

tests/fixtures/contracts/ holds real JSON from a simulated run (made by
scripts/export_contract_examples.py). Each file must still validate against its model and
serialize back to the same JSON; renaming or removing a field by accident fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from recap.core.models import (
    AgentFinding,
    Approval,
    Evidence,
    ExperienceEpisode,
    Incident,
    MitigationProposal,
    RecoveryResult,
    RunRecord,
    SafetyDecision,
    StrategyDecision,
    TraceEvent,
    VerificationReport,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"
CONTRACTS: dict[str, type[BaseModel]] = {
    "incident": Incident,
    "evidence": Evidence,
    "strategy_decision": StrategyDecision,
    "agent_finding": AgentFinding,
    "verification_report": VerificationReport,
    "mitigation_proposal": MitigationProposal,
    "safety_decision": SafetyDecision,
    "approval": Approval,
    "recovery_result": RecoveryResult,
    "experience_episode": ExperienceEpisode,
    "trace_event": TraceEvent,
    "run_record": RunRecord,
}


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_example_validates_and_round_trips(name: str) -> None:
    raw = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    model = CONTRACTS[name].model_validate(raw)
    assert json.loads(model.model_dump_json()) == raw


def test_every_example_has_a_contract() -> None:
    assert {p.stem for p in EXAMPLES.glob("*.json")} == set(CONTRACTS)


def test_the_example_run_is_the_simulated_happy_path() -> None:
    record = RunRecord.model_validate_json((EXAMPLES / "run_record.json").read_text("utf-8"))
    assert record.state.value == "RESOLVED"
    assert record.environment and record.environment.is_simulated
    assert record.recovery and record.recovery.verified
