"""Tests for routing signals: symptom categories, memory signals and budget numbers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from recap.core.config import RoutingSettings
from recap.core.models import (
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    Incident,
    MemoryHit,
    MemoryPartition,
    RoutingSignals,
    RunBudget,
)
from recap.harness.budget import BudgetTracker
from recap.routing.signals import compute_signals, symptom_categories

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
ROUTING = RoutingSettings(familiar_similarity=0.6, conflict_similarity=0.45)
SUCCESS, FAILED = EpisodeOutcome.SUCCESS, EpisodeOutcome.FAILED
SYMPTOMS = ["checkout: HTTP 5xx ratio above 30% (SLO 1%)", "checkout: p95 latency above 1.5s"]


def incident(service: str = "checkout", affected: list[str] | None = None) -> Incident:
    return Incident(
        incident_id="inc-1",
        scenario_id="test",
        service=service,
        affected_services=affected if affected is not None else [service],
        symptoms=SYMPTOMS,
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )


def hit(
    similarity: float,
    outcome: EpisodeOutcome = SUCCESS,
    *,
    service: str = "checkout",
    verified: bool = True,
    mechanism: FailureMechanism = FailureMechanism.BAD_DEPLOYMENT,
) -> MemoryHit:
    """A retrieved past episode; by default a verified, successful bad-deployment fix."""
    episode = ExperienceEpisode(
        episode_id=f"ep-{similarity}-{outcome.value}-{mechanism.value}",
        origin_run_id="run-old",
        scenario_id="old",
        failure_family=mechanism.value,
        partition=MemoryPartition.DEMO,
        service=service,
        symptoms=SYMPTOMS,
        failure_mechanism=mechanism,
        outcome=outcome,
        verified=verified,
        created_at=T0,
    )
    return MemoryHit(episode=episode, similarity=similarity, outcome=outcome)


def signals_for(
    hits: list[MemoryHit], *, case: Incident | None = None, prior_failed_attempts: int = 0
) -> RoutingSignals:
    return compute_signals(
        incident=case or incident(),
        memory_hits=hits,
        budget=BudgetTracker(RunBudget()),
        routing=ROUTING,
        prior_failed_attempts=prior_failed_attempts,
    )


@pytest.mark.parametrize(
    ("symptoms", "expected"),
    [
        (SYMPTOMS, ["errors", "latency"]),
        (["inventory: readiness probe failures on 1 of 2 pods"], ["availability", "errors"]),
        (["payment: pods OOMKilled", "payment: memory near limit"], ["resource"]),
        (
            ["cart: down", "cart: slow pages", "cart: CPU at 95%"],
            ["availability", "latency", "resource"],
        ),
        (["frontend: downstream timeouts in the room"], []),  # "down"/"oom" must start a word
        ([], []),
    ],
)
def test_symptom_categories(symptoms: list[str], expected: list[str]) -> None:
    assert symptom_categories(symptoms) == expected


def test_services_affected_counts_each_service_once() -> None:
    case = incident("checkout", ["checkout", "frontend"])
    assert signals_for([], case=case).services_affected == 2


def test_a_verified_success_on_the_same_service_is_familiar() -> None:
    signals = signals_for([hit(0.82), hit(0.3)])
    assert signals.familiar_success is True
    assert (signals.memory_hits, signals.top_memory_similarity) == (2, 0.82)


@pytest.mark.parametrize(
    "candidate",
    [
        hit(0.5),  # not similar enough
        hit(0.9, verified=False),  # the oracle never verified that recovery
        hit(0.9, service="payment"),  # a different service
        hit(0.9, FAILED),  # the past run did not recover
    ],
)
def test_familiar_needs_a_similar_verified_success_on_the_same_service(
    candidate: MemoryHit,
) -> None:
    assert signals_for([candidate]).familiar_success is False


def test_similar_episodes_that_disagree_are_a_conflict() -> None:
    pressure = hit(0.5, mechanism=FailureMechanism.RESOURCE_PRESSURE)
    assert signals_for([hit(0.7), pressure]).memory_conflict is True
    assert signals_for([hit(0.7), hit(0.3, FAILED)]).memory_conflict is False  # 0.3 is not similar


def test_similar_failed_episodes_count_as_prior_failed_attempts() -> None:
    signals = signals_for([hit(0.7, FAILED), hit(0.4, FAILED)], prior_failed_attempts=1)
    assert signals.prior_failed_attempts == 2  # the given one + the similar failed episode


def test_budget_numbers_come_from_the_tracker() -> None:
    budget = BudgetTracker(RunBudget(max_steps=10, max_tool_calls=12, max_parallel_agents=3))
    budget.charge_step()
    budget.reserve_tool_call()
    budget.reserve_tool_call()
    signals = compute_signals(
        incident=incident(), memory_hits=[], budget=budget, routing=ROUTING, escalation=True
    )
    assert (signals.remaining_steps, signals.remaining_tool_calls) == (9, 10)
    assert signals.max_parallel_agents == 3 and signals.escalation is True
    assert signals.familiar_success is False and signals.top_memory_similarity == 0.0
