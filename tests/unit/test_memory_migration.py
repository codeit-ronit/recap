"""Opening an older memory file: episodes a real model wrote to DEMO before M3 move to LIVE.

Before M3 every run wrote the DEMO partition, so the published M2 runs (ep-98da966f41 from
qwen3:8b, ep-c7ed0063c0 from Groq's gpt-oss-120b) sit next to the scripted policy's own
episodes: a scripted run reads them, and a model run (TRAIN + LIVE) never does. Opening the
store moves them once, in the partition column and in the stored episode, and records each
move. The scripted policy's episodes, and anything since M3, stay where they are.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from recap.core.db import connect
from recap.core.models import (
    ActionRecord,
    EpisodeOutcome,
    ExperienceEpisode,
    FailureMechanism,
    MemoryPartition,
)
from recap.memory.similarity import MemoryQuery
from recap.memory.store import ExperienceStore, MemoryAccess

T0 = datetime(2026, 10, 21, 9, 43, tzinfo=UTC)
SYMPTOMS = ["checkout: HTTP 5xx ratio above 30% (SLO 1%)", "checkout: p95 latency above 1.5s"]
QUERY = MemoryQuery(service="checkout", affected_services=["checkout"], symptoms=SYMPTOMS)
DEMO, LIVE, TRAIN = MemoryPartition.DEMO, MemoryPartition.LIVE, MemoryPartition.TRAIN
RED_TEAM = "+prompt-injected(red-team)"  # what M2's red-team planner added to a provider name

# The memory table as M2 created it (recap/memory/store.py at v0.3.0).
M2_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_id     TEXT PRIMARY KEY,
    origin_run_id  TEXT NOT NULL,
    scenario_id    TEXT NOT NULL,
    failure_family TEXT NOT NULL,
    partition      TEXT NOT NULL,
    service        TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    verified       INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    json           TEXT NOT NULL
)
"""
# Fields consolidator-v2 (M3) added: an M2 episode's stored JSON has none of them.
M3_FIELDS = {
    "summary_method",
    "trajectory",
    "checked_claims",
    "diagnosis_basis",
    "fix_preconditions",
    "embedding",
    "embedding_model",
}


def episode(
    episode_id: str,
    provider: str | None,
    *,
    partition: MemoryPartition = DEMO,
    created_by: str = "consolidator-v1",
    minutes: int = 0,
    **provenance: object,
) -> ExperienceEpisode:
    """A verified checkout rollback, with the provenance consolidator-v1 gave every episode."""
    fix = ActionRecord(
        action="execute_rollback",
        target="checkout",
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
        service="checkout",
        symptoms=SYMPTOMS,
        failure_mechanism=FailureMechanism.BAD_DEPLOYMENT,
        mechanism_target="checkout",
        successful_action=fix,
        outcome=EpisodeOutcome.SUCCESS,
        verified=True,
        verification_source="environment-health-oracle",
        provenance={
            "simulated": True,
            "environment": "simulated",
            "provider": provider,
            "seed": 11,
            "origin_run_id": f"run-{episode_id}",
            "created_by": created_by,
            "trace": f"runs/run-{episode_id}/events",
            **provenance,
        },
        created_at=T0 + timedelta(minutes=minutes),
    )


def m2_store(path: Path, *episodes: ExperienceEpisode) -> None:
    """Write `episodes` the way M2's ExperienceStore.add did (no M3 code involved)."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(M2_SCHEMA)
        for item in episodes:
            conn.execute(
                "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.episode_id,
                    item.origin_run_id,
                    item.scenario_id,
                    item.failure_family,
                    item.partition.value,
                    item.service,
                    item.outcome.value,
                    int(item.verified),
                    item.created_at.isoformat(timespec="microseconds"),
                    item.model_dump_json(exclude=M3_FIELDS),
                ),
            )
    conn.close()


def stored_partitions(path: Path) -> dict[str, tuple[str, str | None]]:
    """episode ID -> (the partition column, the partition in the stored JSON)."""
    conn = connect(path)
    rows = conn.execute("SELECT episode_id, partition, json FROM episodes").fetchall()
    conn.close()
    return {row["episode_id"]: (row["partition"], kept(row["json"])) for row in rows}


def kept(text: str) -> str | None:
    try:
        return str(json.loads(text)["partition"])
    except ValueError:
        return None  # not an episode at all


def moves(path: Path) -> list[tuple[str, str, str]]:
    conn = connect(path)
    rows = conn.execute(
        "SELECT episode_id, from_partition, to_partition, moved_at FROM partition_moves"
        " ORDER BY episode_id"
    ).fetchall()
    conn.close()
    assert all(row["moved_at"] for row in rows)  # when it moved is on record
    return [(row["episode_id"], row["from_partition"], row["to_partition"]) for row in rows]


def found(store: ExperienceStore, access: MemoryAccess) -> set[str]:
    return {hit.episode.episode_id for hit in store.search(QUERY, access, k=20)}


def test_model_episodes_m2_wrote_to_demo_move_to_live_once(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    m2 = [
        episode("ep-98da966f41", "ollama:qwen3:8b"),  # the published M2 runs
        episode("ep-c7ed0063c0", "groq:openai/gpt-oss-120b", minutes=1),
        episode("ep-replayed", "replay:ollama:qwen3:8b", minutes=2),  # a recorded model's answers
        episode("ep-model-red-team", "ollama:qwen3:8b" + RED_TEAM, minutes=3),
        episode("ep-scripted", "deterministic-scripted-policy", minutes=4),
        episode("ep-scripted-red-team", "deterministic-scripted-policy" + RED_TEAM, minutes=5),
        episode("ep-train", "ollama:qwen3:8b", partition=TRAIN, minutes=6),  # the evaluation's
    ]
    m2_store(path, *m2)

    store = ExperienceStore(path)

    assert stored_partitions(path) == {
        "ep-98da966f41": ("live", "live"),
        "ep-c7ed0063c0": ("live", "live"),
        "ep-replayed": ("live", "live"),
        "ep-model-red-team": ("live", "live"),
        "ep-scripted": ("demo", "demo"),
        "ep-scripted-red-team": ("demo", "demo"),
        "ep-train": ("train", "train"),
    }
    model_runs = {"ep-98da966f41", "ep-c7ed0063c0", "ep-replayed", "ep-model-red-team"}
    assert found(store, MemoryAccess.for_origin("scripted")) == {
        "ep-scripted",
        "ep-scripted-red-team",
        "ep-train",
    }
    assert found(store, MemoryAccess.for_origin("local_llm")) == model_runs | {"ep-train"}
    # Only the partition changed: the episode is otherwise exactly what M2 stored.
    for before in m2:
        after = store.get(before.episode_id)
        assert after is not None
        assert after.model_copy(update={"partition": before.partition}) == before
    recorded = [(episode_id, "demo", "live") for episode_id in sorted(model_runs)]
    assert moves(path) == recorded

    # Opening the file again (every start of RECAP does) changes nothing and records nothing.
    ExperienceStore(path)
    assert moves(path) == recorded
    assert stored_partitions(path)["ep-98da966f41"] == ("live", "live")


def test_since_m3_an_episode_stays_in_the_partition_its_run_chose(tmp_path: Path) -> None:
    """Since M3 a run's partition follows its origin, so a model episode in DEMO is there
    because its run asked for DEMO; an episode with no such provenance proves nothing."""
    path = tmp_path / "memory.db"
    chosen = episode(
        "ep-chosen", "ollama:qwen3:8b", created_by="consolidator-v2", origin="local_llm"
    )
    assert ExperienceStore(path).add(chosen, MemoryAccess.default())  # its run chose DEMO
    m2_store(path, episode("ep-planted", None, created_by=""))

    store = ExperienceStore(path)
    assert {e.episode_id: e.partition for e in store.list()} == {
        "ep-chosen": DEMO,
        "ep-planted": DEMO,
    }
    assert moves(path) == []


def test_an_unreadable_row_never_stops_the_store_from_opening(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    m2_store(path, episode("ep-model", "ollama:qwen3:8b"))
    conn = connect(path)
    with conn:
        conn.execute(
            "INSERT INTO episodes VALUES ('ep-broken', 'run-x', 'bad-deployment', 'x', 'demo',"
            " 'checkout', 'success', 1, '2026-10-21T09:00:00.000000+00:00', 'not json')"
        )
    conn.close()

    ExperienceStore(path)  # opens: the broken row is left as it was
    assert stored_partitions(path)["ep-model"] == ("live", "live")
    assert moves(path) == [("ep-model", "demo", "live")]
