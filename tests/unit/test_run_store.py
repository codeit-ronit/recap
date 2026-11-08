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


def test_one_store_is_safe_to_use_from_many_threads_at_once(tmp_path: Path) -> None:
    """The API's sync endpoints share one RunStore across a thread pool, and a worker settles
    jobs in a thread beside its runs' checkpoints (M6). Without the store's lock, sqlite3's
    shared connection fails this way: "bad parameter or other API misuse", "cannot commit -
    no transaction is active", "no more rows available" (seen in Docker Compose, W3)."""
    import threading

    from recap.core.models import RunConfig, RunRecord
    from recap.harness.store import RunStore

    store = RunStore(tmp_path / "recap.db")
    for n in range(20):
        store.save(RunRecord(run_id=f"run-{n}", scenario_id="bad-deployment", config=RunConfig()))
    errors: list[str] = []

    def use(worker: int) -> None:
        try:
            for step in range(300):
                run_id = f"run-{(worker * step) % 20}"
                if step % 3 == 0:
                    store.save(
                        RunRecord(run_id=run_id, scenario_id="bad-deployment", config=RunConfig())
                    )
                else:
                    store.get(run_id)
                    store.list(5)
        except Exception as exc:  # any error is the race
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=use, args=(n,)) for n in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert len(store.list(100)) == 20
