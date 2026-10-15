"""Sandbox state store: keeps one run's simulated world in its own SQLite file.

Two processes use the simulated environment at the same time: the MCP tool server
(which agents talk to) and the controller (whose recovery oracle checks health). Each
builds its own SimulatedEnvironment object, so the world cannot live in Python memory.
Instead the whole state is one JSON document in `<state_dir>/<run_id>.db`, every
environment method reads it fresh, and every write is a single SQLite transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from recap.core.db import connect
from recap.core.ids import canonical_json

State = dict[str, Any]
T = TypeVar("T")

NOT_RESET = "environment not reset"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sandbox_state ("
    "id INTEGER PRIMARY KEY CHECK (id = 1), "  # exactly one row: the current world
    "state_json TEXT NOT NULL)"
)
_UPSERT = (
    "INSERT INTO sandbox_state (id, state_json) VALUES (1, ?) "
    "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json"
)


class SandboxStateStore:
    """Loads and saves the simulated world of one run (a single JSON row in SQLite)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> State | None:
        """The saved state, or None if the environment was never reset."""
        if not self.path.exists():  # a read must never create files
            return None
        with self._open() as conn:
            return self._read(conn)

    def save(self, state: State) -> None:
        """Replace the whole state in one transaction (used by reset)."""
        with self._open() as conn, conn:  # `with conn` commits, or rolls back on error
            conn.execute(_UPSERT, (canonical_json(state),))

    def update(self, change: Callable[[State], tuple[State, T]]) -> T:
        """Load, change and save the state in ONE transaction and return change's result."""
        with self._open() as conn, conn:
            # IMMEDIATE takes the write lock before reading, so two writers (even in two
            # processes) can never both read the old state and overwrite each other.
            conn.execute("BEGIN IMMEDIATE")
            state = self._read(conn)
            if state is None:
                raise RuntimeError(NOT_RESET)
            new_state, result = change(state)
            conn.execute(_UPSERT, (canonical_json(new_state),))
        return result

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        try:
            conn.execute(_SCHEMA)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _read(conn: sqlite3.Connection) -> State | None:
        row = conn.execute("SELECT state_json FROM sandbox_state WHERE id = 1").fetchone()
        return json.loads(row["state_json"]) if row else None
