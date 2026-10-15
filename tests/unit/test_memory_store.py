"""Tests for ExperienceStore: storage rules, deterministic search and reuse assessment.

Covers the three memory cases the spec asks for: MISSING memory (empty store),
NONMATCHING memory (unrelated incidents) and CONFLICTING memory (similar episodes
that disagree), plus the rule that "similar" is never automatically "safe to reuse".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from recap.core.models import (
    ActionRecord,
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    HypothesisStatus,
    MemoryHit,
    MemoryPartition,
    VerificationReport,
)
from recap.memory.similarity import MemoryQuery
from recap.memory.store import (
    UNCHECKED_REASON,
    ExperienceStore,
    MemoryAccess,
    assess_reuse,
    memory_conflict,
)

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
SUCCESS, FAILED = EpisodeOutcome.SUCCESS, EpisodeOutcome.FAILED
DEPLOY, PRESSURE = FailureMechanism.BAD_DEPLOYMENT, FailureMechanism.RESOURCE_PRESSURE

CHECKOUT_5XX = ["checkout: HTTP 5xx ratio above 30% (SLO 1%)", "checkout: p95 latency above 1.5s"]
PAYMENT_OOM = ["payment: pods restarting with OOMKilled", "payment: memory near container limit"]
QUERY = MemoryQuery(service="checkout", affected_services=["checkout"], symptoms=CHECKOUT_5XX)


def make_episode(
    episode_id: str = "ep-1",
    *,
    service: str = "checkout",
    symptoms: list[str] = CHECKOUT_5XX,
    mechanism: FailureMechanism | None = DEPLOY,
    outcome: EpisodeOutcome = SUCCESS,
    verified: bool = True,
    partition: MemoryPartition = MemoryPartition.DEMO,
    minutes: int = 0,
) -> ExperienceEpisode:
    """A past episode; by default a verified rollback success on checkout."""
    fix = ActionRecord(
        action="execute_rollback",
        target=service,
        params={"to_version": "v2.4.0"},
        result="recovered",
        at=T0,
    )
    return ExperienceEpisode(
        episode_id=episode_id,
        origin_run_id=f"run-{episode_id}",
        scenario_id="bad-deployment",
        failure_family="bad_deployment",
        partition=partition,
        service=service,
        symptoms=list(symptoms),
        failure_mechanism=mechanism,
        mechanism_target=service,
        successful_action=fix if outcome == SUCCESS else None,
        outcome=outcome,
        verified=verified,
        verification_source="environment-health-oracle" if verified else None,
        provenance={"simulated": True},
        created_at=T0 + timedelta(minutes=minutes),
    )


def make_hit(
    similarity: float, mechanism: FailureMechanism | None, outcome: EpisodeOutcome
) -> MemoryHit:
    """A search hit with a given similarity, built without a store."""
    episode = make_episode(mechanism=mechanism, outcome=outcome, verified=outcome == SUCCESS)
    return MemoryHit(episode=episode, similarity=similarity, outcome=outcome)


def live_report(mechanism: FailureMechanism | None) -> VerificationReport:
    """What the evidence verifier concluded from THIS run's evidence (None = undecided)."""
    if mechanism is None:
        return VerificationReport(status="insufficient")
    best = Hypothesis(
        claim="live diagnosis",
        mechanism=mechanism,
        target_service="checkout",
        status=HypothesisStatus.SUPPORTED,
    )
    return VerificationReport(status="supported", best=best)


@pytest.fixture
def store(tmp_path: Path) -> ExperienceStore:
    return ExperienceStore(tmp_path / "memory.db")


def search_one(store: ExperienceStore, episode: ExperienceEpisode) -> MemoryHit:
    """Store `episode` (DEMO) and return the one hit it produces for QUERY."""
    store.add(episode, MemoryAccess.default())
    [only_hit] = store.search(QUERY, MemoryAccess.default())  # fails if not exactly one
    return only_hit


# --- storing ----------------------------------------------------------------------------


def test_round_trip_survives_reopening_the_file(tmp_path: Path) -> None:
    episode = make_episode()
    assert ExperienceStore(tmp_path / "memory.db").add(episode, MemoryAccess.default()) is True

    reopened = ExperienceStore(tmp_path / "memory.db")
    assert reopened.get("ep-1") == episode
    assert reopened.count() == 1


def test_get_unknown_episode_returns_none(store: ExperienceStore) -> None:
    assert store.get("ep-does-not-exist") is None


def test_list_is_newest_first_and_filters_partitions(store: ExperienceStore) -> None:
    train = make_episode("ep-train", partition=MemoryPartition.TRAIN, minutes=5)
    store.add(make_episode("ep-old", minutes=0), MemoryAccess.default())
    store.add(train, MemoryAccess.eval_train())
    store.add(make_episode("ep-new", minutes=10), MemoryAccess.default())

    assert [e.episode_id for e in store.list()] == ["ep-new", "ep-train", "ep-old"]
    assert [e.episode_id for e in store.list([MemoryPartition.TRAIN])] == ["ep-train"]
    assert [e.episode_id for e in store.list([MemoryPartition.DEMO], limit=1)] == ["ep-new"]
    assert store.list([]) == []


def test_test_partition_episodes_are_never_stored(store: ExperienceStore) -> None:
    access = MemoryAccess(
        readable_partitions=[MemoryPartition.TRAIN], write_partition=MemoryPartition.TEST
    )
    with pytest.raises(ValueError, match="test episodes are never stored"):
        store.add(make_episode(partition=MemoryPartition.TEST), access)
    assert store.count() == 0


def test_episode_must_match_the_write_partition(store: ExperienceStore) -> None:
    with pytest.raises(ValueError, match="does not match"):
        store.add(make_episode(partition=MemoryPartition.DEMO), MemoryAccess.eval_train())
    assert store.count() == 0


def test_success_without_verification_is_refused(store: ExperienceStore) -> None:
    with pytest.raises(ValueError, match="independent recovery verification"):
        store.add(make_episode(outcome=SUCCESS, verified=False), MemoryAccess.default())
    assert store.count() == 0


def test_failed_run_is_kept_as_a_cautionary_case(store: ExperienceStore) -> None:
    failed = make_episode(outcome=FAILED, verified=False)
    assert store.add(failed, MemoryAccess.default()) is True
    assert store.get("ep-1") == failed


def test_write_disabled_returns_false_and_stores_nothing(store: ExperienceStore) -> None:
    read_only = MemoryAccess.default().model_copy(update={"write_enabled": False})
    assert store.add(make_episode(), read_only) is False
    assert store.count() == 0


# --- searching --------------------------------------------------------------------------


def test_missing_memory_returns_no_hits(store: ExperienceStore) -> None:
    assert store.search(QUERY, MemoryAccess.default()) == []


def test_nonmatching_memory_returns_no_hits(store: ExperienceStore) -> None:
    store.add(make_episode(service="payment", symptoms=PAYMENT_OOM), MemoryAccess.default())
    assert store.search(QUERY, MemoryAccess.default()) == []


def test_search_orders_by_similarity_then_newest_then_id(store: ExperienceStore) -> None:
    partial = ["checkout: HTTP 5xx ratio above 30%"]
    for episode in [
        make_episode("ep-a", minutes=0),
        make_episode("ep-c", minutes=10),
        make_episode("ep-b", minutes=10),  # same time as ep-c: the ID breaks the tie
        make_episode("ep-weak", symptoms=partial, minutes=20),  # newest, but less similar
    ]:
        store.add(episode, MemoryAccess.default())

    hits = store.search(QUERY, MemoryAccess.default(), k=10)
    assert [h.episode.episode_id for h in hits] == ["ep-b", "ep-c", "ep-a", "ep-weak"]
    assert [h.similarity for h in hits] == [1.0, 1.0, 1.0, 0.7]

    top_two = store.search(QUERY, MemoryAccess.default(), k=2)
    assert [h.episode.episode_id for h in top_two] == ["ep-b", "ep-c"]


def test_min_similarity_is_an_inclusive_cutoff(store: ExperienceStore) -> None:
    partial = make_episode(symptoms=["checkout: HTTP 5xx ratio above 30%"])  # scores 0.7
    store.add(partial, MemoryAccess.default())
    assert len(store.search(QUERY, MemoryAccess.default(), min_similarity=0.7)) == 1
    assert store.search(QUERY, MemoryAccess.default(), min_similarity=0.71) == []


def test_hits_start_similar_but_unchecked(store: ExperienceStore) -> None:
    found = search_one(store, make_episode(outcome=FAILED, verified=False))
    assert found.mechanism_match is None
    assert found.safe_to_reuse is False
    assert found.outcome == FAILED
    assert found.reason == UNCHECKED_REASON


def test_conflicting_memory_is_surfaced_and_flagged(store: ExperienceStore) -> None:
    rollback_worked = make_episode("ep-rollback-worked", minutes=0)
    restart_failed = make_episode(
        "ep-restart-failed", mechanism=PRESSURE, outcome=FAILED, verified=False, minutes=5
    )
    store.add(rollback_worked, MemoryAccess.default())
    store.add(restart_failed, MemoryAccess.default())

    hits = store.search(QUERY, MemoryAccess.default())
    # Both similar episodes are shown with their own outcome, and the conflict is flagged.
    assert {(h.episode.episode_id, h.outcome) for h in hits} == {
        ("ep-rollback-worked", SUCCESS),
        ("ep-restart-failed", FAILED),
    }
    assert memory_conflict(hits, threshold=0.45) is True


# --- memory_conflict --------------------------------------------------------------------


def test_agreeing_or_single_hits_are_not_a_conflict() -> None:
    agreeing = [make_hit(0.9, DEPLOY, SUCCESS), make_hit(0.8, DEPLOY, SUCCESS)]
    assert memory_conflict(agreeing, 0.45) is False
    assert memory_conflict([make_hit(0.9, DEPLOY, SUCCESS)], 0.45) is False
    assert memory_conflict([], 0.45) is False


def test_outcome_disagreement_is_a_conflict() -> None:
    hits = [make_hit(0.9, DEPLOY, SUCCESS), make_hit(0.8, DEPLOY, FAILED)]
    assert memory_conflict(hits, 0.45) is True


def test_only_hits_at_or_above_the_threshold_can_conflict() -> None:
    hits = [make_hit(0.9, DEPLOY, SUCCESS), make_hit(0.3, PRESSURE, FAILED)]
    assert memory_conflict(hits, 0.45) is False
    assert memory_conflict(hits, 0.3) is True


def test_missing_diagnosis_is_not_a_mechanism_disagreement() -> None:
    hits = [make_hit(0.9, DEPLOY, FAILED), make_hit(0.8, None, FAILED)]
    assert memory_conflict(hits, 0.45) is False


# --- assess_reuse -----------------------------------------------------------------------


def test_verified_success_with_matching_mechanism_is_safe(store: ExperienceStore) -> None:
    assessed = assess_reuse(search_one(store, make_episode()), live_report(DEPLOY))
    assert assessed.mechanism_match is True
    assert assessed.safe_to_reuse is True
    assert assessed.reason.startswith("safe to reuse")


def test_similar_but_different_mechanism_is_not_safe(store: ExperienceStore) -> None:
    assessed = assess_reuse(search_one(store, make_episode()), live_report(PRESSURE))
    assert assessed.mechanism_match is False
    assert assessed.safe_to_reuse is False
    assert "live evidence points to resource pressure, not bad deployment" in assessed.reason


def test_failed_episode_is_never_safe_even_if_mechanism_matches(store: ExperienceStore) -> None:
    failed = make_episode(outcome=FAILED, verified=False)
    assessed = assess_reuse(search_one(store, failed), live_report(DEPLOY))
    assert assessed.mechanism_match is True
    assert assessed.safe_to_reuse is False
    assert "ended as failed" in assessed.reason


def test_no_live_diagnosis_means_not_safe(store: ExperienceStore) -> None:
    assessed = assess_reuse(search_one(store, make_episode()), live_report(None))
    assert assessed.mechanism_match is False
    assert assessed.safe_to_reuse is False
    assert "has not confirmed a mechanism" in assessed.reason


def test_assess_reuse_returns_a_new_hit(store: ExperienceStore) -> None:
    original = search_one(store, make_episode())
    assessed = assess_reuse(original, live_report(DEPLOY))
    assert assessed is not original
    assert original.safe_to_reuse is False
    assert original.reason == UNCHECKED_REASON
