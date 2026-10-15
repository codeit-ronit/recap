"""Tests for memory contamination controls.

Evaluation must never learn from the data it is scored on: test runs read TRAIN
episodes only and never write, and the scenario's ground-truth family label never
leaks into what agents are told.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from recap.core.db import connect
from recap.core.models import (
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    Hypothesis,
    HypothesisStatus,
    MemoryPartition,
    RunConfig,
    VerificationReport,
)
from recap.memory.similarity import MemoryQuery
from recap.memory.store import ExperienceStore, MemoryAccess, assess_reuse

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)
SYMPTOMS = ["checkout: HTTP 5xx ratio above 30% (SLO 1%)", "checkout: p95 latency above 1.5s"]
QUERY = MemoryQuery(service="checkout", affected_services=["checkout"], symptoms=SYMPTOMS)
TRAIN, TEST, DEMO = MemoryPartition.TRAIN, MemoryPartition.TEST, MemoryPartition.DEMO


def make_episode(
    episode_id: str,
    partition: MemoryPartition,
    *,
    family: str = "bad_deployment",
    minutes: int = 0,
) -> ExperienceEpisode:
    """A verified rollback success on checkout, identical apart from its labels."""
    return ExperienceEpisode(
        episode_id=episode_id,
        origin_run_id=f"run-{episode_id}",
        scenario_id="bad-deployment",
        failure_family=family,
        partition=partition,
        service="checkout",
        symptoms=SYMPTOMS,
        failure_mechanism=FailureMechanism.BAD_DEPLOYMENT,
        mechanism_target="checkout",
        outcome=EpisodeOutcome.SUCCESS,
        verified=True,
        verification_source="environment-health-oracle",
        created_at=T0 + timedelta(minutes=minutes),
    )


def plant_row(store: ExperienceStore, episode: ExperienceEpisode) -> None:
    """Insert a row with raw SQL, bypassing `add` (as if the file had been tampered with)."""
    conn = connect(store.db_path)
    with conn:
        conn.execute(
            "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode.episode_id,
                episode.origin_run_id,
                episode.scenario_id,
                episode.failure_family,
                episode.partition.value,
                episode.service,
                episode.outcome.value,
                int(episode.verified),
                episode.created_at.isoformat(timespec="microseconds"),
                episode.model_dump_json(),
            ),
        )
    conn.close()


@pytest.fixture
def mixed_store(tmp_path: Path) -> ExperienceStore:
    """One identical episode in each partition (the TEST one planted behind `add`'s back)."""
    store = ExperienceStore(tmp_path / "memory.db")
    store.add(make_episode("ep-train", TRAIN, minutes=0), MemoryAccess.eval_train())
    store.add(make_episode("ep-demo", DEMO, minutes=1), MemoryAccess.default())
    plant_row(store, make_episode("ep-test", TEST, minutes=2))
    assert store.count() == 3
    return store


def found_ids(store: ExperienceStore, access: MemoryAccess) -> set[str]:
    return {hit.episode.episode_id for hit in store.search(QUERY, access, k=10)}


def test_eval_test_access_never_sees_demo_or_test_episodes(mixed_store: ExperienceStore) -> None:
    assert found_ids(mixed_store, MemoryAccess.eval_test()) == {"ep-train"}


def test_eval_test_access_never_writes(mixed_store: ExperienceStore) -> None:
    access = MemoryAccess.eval_test()
    assert mixed_store.add(make_episode("ep-from-test-run", TRAIN), access) is False
    assert mixed_store.add(make_episode("ep-from-test-run", TEST), access) is False
    assert mixed_store.count() == 3


def test_eval_test_write_partition_is_test_so_a_forced_write_fails(tmp_path: Path) -> None:
    forced = MemoryAccess.eval_test().model_copy(update={"write_enabled": True})
    with pytest.raises(ValueError, match="never stored"):
        ExperienceStore(tmp_path / "m.db").add(make_episode("ep-x", TEST), forced)


def test_eval_train_reads_and_writes_train_only(mixed_store: ExperienceStore) -> None:
    access = MemoryAccess.eval_train()
    assert found_ids(mixed_store, access) == {"ep-train"}
    with pytest.raises(ValueError, match="does not match"):
        mixed_store.add(make_episode("ep-demo-2", DEMO), access)


def test_default_access_reads_train_and_demo_but_not_test(mixed_store: ExperienceStore) -> None:
    assert found_ids(mixed_store, MemoryAccess.default()) == {"ep-train", "ep-demo"}


def test_access_from_run_config_copies_the_memory_switches() -> None:
    config = RunConfig(
        memory_read_partitions=[TRAIN], memory_write=False, memory_write_partition=TRAIN
    )
    access = MemoryAccess.from_run_config(config)
    assert access.readable_partitions == [TRAIN]
    assert access.write_enabled is False
    assert access.write_partition == TRAIN
    assert MemoryAccess.from_run_config(RunConfig()) == MemoryAccess.default()


@pytest.mark.parametrize("family", ["bad_deployment", "secret-family-label"])
def test_failure_family_never_appears_in_hit_reasons(tmp_path: Path, family: str) -> None:
    # "bad_deployment" is also a mechanism name: reasons use plain words for mechanisms,
    # so even then the scenario's ground-truth label is never repeated to agents.
    store = ExperienceStore(tmp_path / "memory.db")
    store.add(make_episode("ep-1", DEMO, family=family), MemoryAccess.default())
    hits = store.search(QUERY, MemoryAccess.default())
    assert hits

    reports = [VerificationReport(status="insufficient")] + [
        VerificationReport(
            status="supported",
            best=Hypothesis(claim="live", mechanism=mechanism, status=HypothesisStatus.SUPPORTED),
        )
        for mechanism in FailureMechanism
    ]
    reasons = [hits[0].reason] + [assess_reuse(hits[0], report).reason for report in reports]
    assert all(family not in reason for reason in reasons)
