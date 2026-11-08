"""Run store: saves incident checkpoints (RunRecord) and evidence to SQLite.

The controller saves the run after every lifecycle transition, so a run can always
be inspected from the CLI, the API or the dashboard, and a crash leaves a recorded
state behind. Trace events are stored separately by the SQLite trace sink.
"""

from __future__ import annotations

import threading
from pathlib import Path

from recap.core.db import connect
from recap.core.models import Evidence, RunRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    state TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    run_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (run_id, evidence_id)
);
"""

# A run closed after its process stopped (its record has an `interruption`, M6) is final: a
# later checkpoint without one, from a process that was presumed dead but was not (a frozen
# or partitioned worker), is ignored, so it cannot undo the closing.
_SAVE = (
    "INSERT INTO runs (run_id, scenario_id, state, label, created_at, updated_at, record)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,"
    " updated_at=excluded.updated_at, record=excluded.record"
    " WHERE json_extract(runs.record, '$.interruption') IS NULL"
    " OR json_extract(excluded.record, '$.interruption') IS NOT NULL"
)


class RunStore:
    """Persists RunRecords (checkpoints) and the evidence collected by each run."""

    def __init__(self, db_path: Path | str) -> None:
        self._conn = connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # One connection, used from several threads: the API's sync endpoints run in a thread
        # pool, and a worker settles jobs in a thread beside its runs' checkpoints. sqlite3's
        # connection is not safe for simultaneous use, so every call takes this lock (as
        # SqliteTraceSink and ApprovalStore do).
        self._lock = threading.Lock()

    def save(self, record: RunRecord) -> None:
        """Insert or update the checkpoint of one run (a closed run's stays final: _SAVE)."""
        row = (
            record.run_id,
            record.scenario_id,
            record.state.value,
            record.config.label,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            record.model_dump_json(),
        )
        with self._lock:
            self._conn.execute(_SAVE, row)
            self._conn.commit()

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return RunRecord.model_validate_json(row["record"]) if row else None

    def list(self, limit: int = 100) -> list[RunRecord]:
        """Most recent runs first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT record FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [RunRecord.model_validate_json(r["record"]) for r in rows]

    def add_evidence(self, run_id: str, evidence: Evidence) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO evidence (run_id, evidence_id, record) VALUES (?, ?, ?)",
                (run_id, evidence.id, evidence.model_dump_json()),
            )
            self._conn.commit()

    def evidence(self, run_id: str) -> list[Evidence]:
        with self._lock:
            rows = self._conn.execute(
                # Insertion order: parallel-round IDs ("ev-..-r2t01") do not sort by time.
                "SELECT record FROM evidence WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [Evidence.model_validate_json(r["record"]) for r in rows]
