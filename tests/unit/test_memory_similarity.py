"""Tests for the deterministic word-overlap similarity used by episodic memory."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from recap.core.models import (
    EpisodeOutcome,
    ExperienceEpisode,
    Incident,
    MemoryPartition,
    RunBudget,
)
from recap.memory.similarity import MemoryQuery, episode_similarity, jaccard, tokenize

CHECKOUT_SYMPTOMS = [
    "checkout: HTTP 5xx ratio above 30% (SLO 1%)",
    "checkout: p95 latency above 1.5s",
]


def make_episode(service: str, symptoms: list[str]) -> ExperienceEpisode:
    """A minimal past episode; only `service` and `symptoms` matter for similarity."""
    return ExperienceEpisode(
        episode_id="ep-1",
        origin_run_id="run-1",
        scenario_id="some-scenario",
        failure_family="some-family",
        partition=MemoryPartition.TRAIN,
        service=service,
        symptoms=symptoms,
        outcome=EpisodeOutcome.FAILED,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_tokenize_keeps_meaningful_words() -> None:
    words = tokenize("checkout: HTTP 5xx ratio above 30% (SLO 1%)")
    # "above" is a stopword, "30" is only digits and "1" is too short.
    assert words == {"checkout", "http", "5xx", "ratio", "slo"}


def test_tokenize_drops_short_words_numbers_and_stopwords() -> None:
    assert tokenize("The pod is at 97% and DB 2000") == {"pod"}


def test_tokenize_is_case_insensitive() -> None:
    assert tokenize("OOMKilled Payment") == {"oomkilled", "payment"}
    assert tokenize("OOMKilled Payment") == tokenize("oomkilled payment")


def test_jaccard_values() -> None:
    assert jaccard({"aaa", "bbb"}, {"aaa", "bbb"}) == 1.0
    assert jaccard({"aaa"}, {"bbb"}) == 0.0
    assert jaccard({"aaa", "bbb"}, {"bbb", "ccc"}) == pytest.approx(1 / 3)


def test_jaccard_of_two_empty_sets_is_zero() -> None:
    # Nothing to compare is no evidence of similarity.
    assert jaccard(set(), set()) == 0.0


def test_same_service_and_symptoms_score_one() -> None:
    query = MemoryQuery(service="checkout", symptoms=CHECKOUT_SYMPTOMS)
    assert episode_similarity(query, make_episode("checkout", CHECKOUT_SYMPTOMS)) == 1.0


def test_affected_service_gets_half_the_service_credit() -> None:
    query = MemoryQuery(
        service="frontend", affected_services=["frontend", "checkout"], symptoms=CHECKOUT_SYMPTOMS
    )
    # 0.7 * 1.0 (same symptom words) + 0.3 * 0.5 (checkout is affected, not alerted)
    assert episode_similarity(query, make_episode("checkout", CHECKOUT_SYMPTOMS)) == 0.85


def test_unrelated_service_and_symptoms_score_zero() -> None:
    query = MemoryQuery(service="checkout", symptoms=CHECKOUT_SYMPTOMS)
    unrelated = make_episode("payment", ["payment: pods restarting with OOMKilled"])
    assert episode_similarity(query, unrelated) == 0.0


def test_score_is_rounded_to_three_decimals() -> None:
    query = MemoryQuery(service="payment", symptoms=["payment latency"])
    other = make_episode("cart", ["payment timeouts"])
    # 0.7 * (1 shared word / 3 words) = 0.2333... -> 0.233
    assert episode_similarity(query, other) == 0.233


def test_score_ignores_symptom_order_and_is_repeatable() -> None:
    query = MemoryQuery(service="checkout", symptoms=CHECKOUT_SYMPTOMS)
    reordered = make_episode("checkout", list(reversed(CHECKOUT_SYMPTOMS)))
    scores = {episode_similarity(query, reordered) for _ in range(5)}
    assert scores == {1.0}


def test_query_from_incident_copies_the_comparable_fields() -> None:
    incident = Incident(
        incident_id="inc-1",
        scenario_id="bad-deployment",
        service="checkout",
        affected_services=["checkout", "frontend"],
        symptoms=CHECKOUT_SYMPTOMS,
        created_at=datetime(2026, 3, 14, 10, 0, tzinfo=UTC),
        environment="simulated",
        budget=RunBudget(),
    )
    query = MemoryQuery.from_incident(incident)
    assert query == MemoryQuery(
        service="checkout", affected_services=["checkout", "frontend"], symptoms=CHECKOUT_SYMPTOMS
    )
