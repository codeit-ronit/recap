"""The provider-facing views (audit finding 2.3): labels and oracle data never reach a provider.

A provider (the scripted policy or an LLM) gets an InvestigationRequest or a ProposalRequest.
Those requests hold `AgentIncident` and `AgentMemoryHit` views instead of the full records,
so the scenario ID, the scenario's failure family and the evaluation bookkeeping cannot be
in them. After a failed fix, the next round's focus comes from RECAP's own reasoning
(`_retry_focus`), never from the recovery oracle's list of unhealthy services.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from recap.core.config import load_settings
from recap.core.models import (
    ActionRecord,
    AgentEpisode,
    AgentIncident,
    AgentMemoryHit,
    AgentRole,
    ContextPack,
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    Incident,
    InvestigationRequest,
    MemoryHit,
    MemoryPartition,
    MitigationProposal,
    ProposalRequest,
    RunBudget,
    VerificationReport,
)
from recap.harness.controller import _retry_focus
from recap.tools.registry import ToolRegistry

AT = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
LABEL_KEYS = {"scenario_id", "failure_family", "partition", "origin_run_id", "provenance"}


def incident() -> Incident:
    return Incident(
        incident_id="inc-1",
        scenario_id="bad-deployment",  # the answer key: never for providers
        service="checkout",
        affected_services=["checkout"],
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        created_at=AT,
        environment="simulated",
        metadata={"source": "simulated-alertmanager"},
        budget=RunBudget(),
    )


def hit() -> MemoryHit:
    episode = ExperienceEpisode(
        episode_id="ep-1",
        origin_run_id="run-1",
        scenario_id="bad-deployment",
        failure_family="bad_deployment",
        partition=MemoryPartition.TRAIN,
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        failure_mechanism=FailureMechanism.BAD_DEPLOYMENT,
        mechanism_target="checkout",
        successful_action=ActionRecord(
            action="execute_rollback", target="checkout", result="recovered", at=AT
        ),
        outcome=EpisodeOutcome.SUCCESS,
        verified=True,
        verification_source="environment-health-oracle",
        provenance={"seed": 11, "origin_run_id": "run-1"},
        created_at=AT,
    )
    return MemoryHit(
        episode=episode, similarity=0.9, outcome=EpisodeOutcome.SUCCESS, safe_to_reuse=True
    )


def keys_anywhere(node: Any) -> set[str]:
    """Every dictionary key in a dumped model, at any depth."""
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in keys_anywhere(value)}
    if isinstance(node, list):
        return {key for item in node for key in keys_anywhere(item)}
    return set()


# --- the views ------------------------------------------------------------------------------


def test_the_incident_view_keeps_the_alert_and_drops_the_labels() -> None:
    view = incident().agent_view()

    assert isinstance(view, AgentIncident)
    dumped = view.model_dump(mode="json")
    assert "scenario_id" not in dumped and "metadata" not in dumped
    assert (view.incident_id, view.service, view.symptoms) == (
        "inc-1",
        "checkout",
        ["checkout: HTTP 5xx ratio above 30%"],
    )
    assert view.created_at == AT and view.environment == "simulated"


def test_the_episode_view_keeps_the_story_and_drops_the_labels() -> None:
    view = hit().episode.agent_view()

    assert isinstance(view, AgentEpisode)
    assert not LABEL_KEYS & set(view.model_dump(mode="json"))
    # The past run's own verified conclusion and what it did stay: that is the experience.
    assert view.failure_mechanism == FailureMechanism.BAD_DEPLOYMENT
    assert view.successful_action is not None and view.successful_action.action == (
        "execute_rollback"
    )
    assert view.episode_id == "ep-1"  # providers cite it


def test_the_memory_hit_view_keeps_recaps_reuse_verdict() -> None:
    view = hit().agent_view()

    assert isinstance(view, AgentMemoryHit)
    assert isinstance(view.episode, AgentEpisode)
    assert (view.similarity, view.safe_to_reuse, view.outcome) == (
        0.9,
        True,
        EpisodeOutcome.SUCCESS,
    )
    assert not LABEL_KEYS & keys_anywhere(view.model_dump(mode="json"))


def test_a_view_cannot_carry_a_label_even_when_built_by_hand() -> None:
    with pytest.raises(ValidationError):
        AgentIncident.model_validate({**incident().agent_view().model_dump(), "scenario_id": "x"})
    with pytest.raises(ValidationError):
        AgentEpisode.model_validate(
            {**hit().episode.agent_view().model_dump(), "failure_family": "bad_deployment"}
        )


# --- the requests -----------------------------------------------------------------------------


def test_requests_keep_only_the_views_even_when_given_the_full_records() -> None:
    """A caller that passes the full Incident or MemoryHit still produces a label-free request."""
    pack = ContextPack(budget_tokens=100, tokens_before=0, tokens_after=0)
    investigation = InvestigationRequest(
        role=AgentRole.INVESTIGATOR,
        goal="find the root cause",
        incident=incident(),  # the full record...
        context=pack,
        step=1,
        max_steps=4,
        memory_hints=[hit()],
    )
    proposal = ProposalRequest(
        incident=incident(),
        hypothesis=Hypothesis(claim="bad release", mechanism=FailureMechanism.BAD_DEPLOYMENT),
        report=VerificationReport(status="supported"),
        context=pack,
        memory_hits=[hit()],
    )

    for request in (investigation, proposal):
        assert isinstance(request.incident, AgentIncident)  # ...is reduced to its view
        keys = keys_anywhere(request.model_dump(mode="json"))
        assert not (LABEL_KEYS | {"metadata"}) & keys
        assert "bad-deployment" not in request.model_dump_json()  # the scenario ID nowhere


# --- the retry focus --------------------------------------------------------------------------


def registry() -> ToolRegistry:
    return ToolRegistry(load_settings().policies)


def proposal(action: str, target: str) -> MitigationProposal:
    return MitigationProposal(
        proposal_id="prop-1",
        incident_id="inc-1",
        action=action,
        target=target,
        rationale="test",
    )


def test_the_retry_focus_is_the_failed_target_plus_the_verifiers_services() -> None:
    report = VerificationReport(status="supported", focus_services=["payment", "checkout"])

    focus = _retry_focus(registry(), proposal("restart_service", "checkout"), report)

    assert focus == ["checkout", "payment"]  # deduplicated, the fix's target first


def test_the_retry_focus_cannot_see_the_oracles_unhealthy_services() -> None:
    """Structural guard: the focus is computed from the fix and the verifier's report only.
    If someone ever passes the oracle's probe in (its list of unhealthy services is ground
    truth no agent observed), this test fails and points at audit finding 2.3."""
    assert list(inspect.signature(_retry_focus).parameters) == ["registry", "proposal", "report"]
    report = VerificationReport(status="supported", focus_services=[])

    focus = _retry_focus(registry(), proposal("execute_rollback", "checkout"), report)

    assert focus == ["checkout"]  # only the failed fix's own target


def test_a_namespace_wide_action_adds_no_service_to_the_focus() -> None:
    report = VerificationReport(status="supported", focus_services=["payment"])

    assert _retry_focus(registry(), proposal("delete_namespace", "shop"), report) == ["payment"]
