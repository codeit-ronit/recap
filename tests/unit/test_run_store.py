"""Unit tests for the SQLite run store (checkpoints and evidence)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from recap.context.evidence import EvidenceRegistry
from recap.core.models import IncidentState, RunConfig, RunRecord, SourceType
from recap.harness.store import RunStore


def test_checkpoints_round_trip_and_update_in_place(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "recap.db")
    record = RunRecord(run_id="run-a", scenario_id="bad-deployment", config=RunConfig(label="x"))
    store.save(record)
    record.state = IncidentState.RESOLVED
    store.save(record)  # the same run is updated, not duplicated

    loaded = store.get("run-a")
    assert loaded == record
    assert [r.run_id for r in store.list()] == ["run-a"]
    assert store.get("run-missing") is None


def test_list_is_newest_first(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "recap.db")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(3):
        store.save(
            RunRecord(
                run_id=f"run-{i}",
                scenario_id="s",
                config=RunConfig(),
                created_at=t0 + timedelta(minutes=i),
            )
        )
    assert [r.run_id for r in store.list(limit=2)] == ["run-2", "run-1"]


def test_evidence_is_persisted_per_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "recap.db")
    registry = EvidenceRegistry(
        "run-ev00000001", "inc-1", on_add=lambda ev: store.add_evidence("run-ev00000001", ev)
    )
    first = registry.add(source_type=SourceType.TOOL, source_ref="mcp:a()", content="one")
    registry.add(source_type=SourceType.TOOL, source_ref="mcp:b()", content="two")

    saved = store.evidence("run-ev00000001")
    assert [e.id for e in saved] == registry.ids()
    assert saved[0] == first
    assert store.evidence("run-other") == []
