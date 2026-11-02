"""The Kind adapter's own local state: never in the cluster, never shown to a model.

Three things live in RECAP's data folder (the state folder load_settings passes in):

    kind-<run_id>.db     one run: the executor's public key, the used capability nonces,
                         the run's baseline and the changes made during it (the release
                         log), the audit of every write and of every kubectl command
    kind-leases.db       which run holds the demo namespace (one live run at a time)
    kind-writes-disabled the write switch: while this file exists every write is refused

Two processes serve one run: the controller (reset, the oracle) and the MCP server
subprocess (the tools and the write path). Both build their own adapter, so everything is
read fresh from these files, and every change is one SQLite transaction.

Why the nonce is committed BEFORE the cluster is touched: a Kubernetes write cannot share a
transaction with a local file. Recording the nonce first fails closed: if the process dies
between the commit and the write, the capability is burnt and the write never happened,
but a capability can never be used twice. The write, its audit row and the commands are
append-only (triggers refuse updates and deletes: a guard against bugs and slips, not
against someone who can rewrite the file, who could also bind a key of their own).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from recap.core.db import connect
from recap.core.errors import CapabilityError
from recap.core.ids import utcnow
from recap.core.signals import as_utc
from recap.environments.kubernetes.kubectl import CommandRecord
from recap.safety.capabilities import CapabilityPayload

NOT_RESET = "environment not reset"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, used_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    service TEXT NOT NULL,
    version TEXT NOT NULL,
    previous_version TEXT,
    kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    proposal_digest TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    identity TEXT NOT NULL,
    op TEXT NOT NULL,
    argv TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    output_bytes INTEGER NOT NULL,
    error TEXT NOT NULL
);
"""
_APPEND_ONLY = ("nonces", "releases", "writes", "commands")


def _guards() -> str:
    statements = []
    for table in _APPEND_ONLY:
        for verb in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{verb.lower()} BEFORE {verb} ON "
                f"{table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    return "\n".join(statements)


@dataclass(frozen=True)
class Baseline:
    """What one service ran when this run's baseline was confirmed healthy."""

    version: str
    revision: int


@dataclass(frozen=True)
class RunInfo:
    """The parts of a run's state the read methods need."""

    run_id: str
    baseline_at: datetime
    observation_start: datetime  # observations older than this belong to earlier runs
    baselines: dict[str, Baseline]


@dataclass(frozen=True)
class ReleaseEntry:
    """A change this run made or saw happen: a fault's release, a rollback, a restart."""

    at: datetime
    service: str
    version: str
    previous_version: str | None
    kind: str  # "release" (a scenario's fault step), "rollback" or "restart"


class RunState:
    """The per-run SQLite file. Reads never create it; only reset does."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.is_file()

    # --- lifecycle ----------------------------------------------------------------------

    def start(self, **meta: Any) -> None:
        """Begin (or begin again) a run: a fresh meta table; used nonces and audits stay."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM meta")
            for key, value in meta.items():
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, _dump(value)))

    def put(self, **meta: Any) -> None:
        with self._transaction() as conn:
            for key, value in meta.items():
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, _dump(value)),
                )

    def get(self, key: str) -> Any:
        return self.meta().get(key)

    def meta(self) -> dict[str, Any]:
        """Every meta value, read in one go ({} when the file does not exist)."""
        if not self.exists():
            return {}
        with self._open() as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def info(self) -> RunInfo:
        """The run's baseline; RuntimeError("environment not reset") before a complete reset."""
        meta = self.meta()
        if not meta.get("reset_complete"):
            raise RuntimeError(NOT_RESET)
        baselines = {
            service: Baseline(version=str(entry["version"]), revision=int(entry["revision"]))
            for service, entry in (meta.get("baselines") or {}).items()
        }
        return RunInfo(
            run_id=str(meta.get("run_id")),
            baseline_at=_time(meta.get("baseline_at")),
            observation_start=_time(meta.get("observation_start")),
            baselines=baselines,
        )

    # --- the write boundary -------------------------------------------------------------

    def bind_key(self, public_key: str) -> None:
        """Trust capabilities signed by this run's key (the controller, right after reset)."""
        self.info()  # refuses before a complete reset
        self.put(executor_key=public_key)

    def consume_capability(
        self, verify: Callable[[str | None, set[str]], CapabilityPayload]
    ) -> CapabilityPayload:
        """Verify a capability against the bound key and the used nonces, and COMMIT its
        nonce, in one exclusive transaction: before anything is sent to the cluster."""
        if not self.exists():
            raise CapabilityError(f"no executor key is bound ({NOT_RESET})")
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")  # the write lock first: two writers serialize
            try:
                row = conn.execute("SELECT value FROM meta WHERE key = 'executor_key'").fetchone()
                key = json.loads(row["value"]) if row else None
                used = {r["nonce"] for r in conn.execute("SELECT nonce FROM nonces")}
                payload = verify(key, used)
                conn.execute(
                    "INSERT INTO nonces (nonce, used_at) VALUES (?, ?)",
                    (payload.nonce, utcnow().isoformat()),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return payload

    def used_nonces(self) -> set[str]:
        if not self.exists():
            return set()
        with self._open() as conn:
            return {row["nonce"] for row in conn.execute("SELECT nonce FROM nonces")}

    # --- what happened during the run ---------------------------------------------------

    def log_release(
        self,
        service: str,
        version: str,
        previous_version: str | None,
        kind: str,
        at: datetime | None = None,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO releases (at, service, version, previous_version, kind) "
                "VALUES (?, ?, ?, ?, ?)",
                ((at or utcnow()).isoformat(), service, version, previous_version, kind),
            )

    def release_log(self, service: str | None = None) -> list[ReleaseEntry]:
        if not self.exists():
            return []
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM releases ORDER BY id").fetchall()
        entries = [
            ReleaseEntry(
                at=_time(row["at"]),
                service=row["service"],
                version=row["version"],
                previous_version=row["previous_version"],
                kind=row["kind"],
            )
            for row in rows
        ]
        return [entry for entry in entries if service in (None, entry.service)]

    def record_write(
        self,
        *,
        action: str,
        target: str,
        approval_id: str,
        proposal_digest: str,
        outcome: str,
        detail: dict[str, Any],
    ) -> None:
        """One attempted write: who approved it, what happened. Never the capability."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO writes (at, action, target, approval_id, proposal_digest, outcome, "
                "detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    utcnow().isoformat(),
                    action,
                    target,
                    approval_id,
                    proposal_digest,
                    outcome,
                    _dump(detail),
                ),
            )

    def writes(self) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM writes ORDER BY id").fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def record_command(self, record: CommandRecord) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO commands (at, identity, op, argv, exit_code, duration_ms, "
                "output_bytes, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.at.isoformat(),
                    record.identity,
                    record.op,
                    _dump(record.argv),
                    record.exit_code,
                    record.duration_ms,
                    record.output_bytes,
                    record.error,
                ),
            )

    def commands(self) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self._open() as conn:
            rows = conn.execute("SELECT * FROM commands ORDER BY id").fetchall()
        return [{**dict(row), "argv": json.loads(row["argv"])} for row in rows]

    # --- plumbing -----------------------------------------------------------------------

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        conn.isolation_level = None  # explicit BEGIN/COMMIT only
        try:
            conn.executescript(_SCHEMA + _guards())
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")


class LeaseHeld(RuntimeError):
    """Another run holds the namespace: two runs resetting one demo would break each other."""


@dataclass(frozen=True)
class Lease:
    namespace: str
    run_id: str
    expires_at: datetime


class NamespaceLease:
    """One live run per namespace. The lease expires on its own after `seconds`, because the
    environment interface has no "run finished" call (docs/m5-integration-plan.md, 3.9)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def take(
        self, namespace: str, run_id: str, seconds: float, now: datetime | None = None
    ) -> None:
        """Take or renew the lease for `run_id`; LeaseHeld while another run's is active."""
        moment = as_utc(now or utcnow())
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT run_id, expires_at FROM leases WHERE namespace = ?", (namespace,)
            ).fetchone()
            if row and row["run_id"] != run_id and _time(row["expires_at"]) > moment:
                raise LeaseHeld(
                    f"namespace {namespace} is leased by run {row['run_id']} until "
                    f"{row['expires_at']}; wait for it to end, or release the lease"
                )
            conn.execute(
                "INSERT INTO leases (namespace, run_id, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(namespace) DO UPDATE SET run_id = excluded.run_id, "
                "expires_at = excluded.expires_at",
                (namespace, run_id, (moment + timedelta(seconds=seconds)).isoformat()),
            )

    def release(self, namespace: str, run_id: str) -> bool:
        """Give the lease up (only its holder can); True if it was released."""
        if not self.path.is_file():
            return False
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM leases WHERE namespace = ? AND run_id = ?", (namespace, run_id)
            )
            return cursor.rowcount > 0

    def holder(self, namespace: str) -> Lease | None:
        """Who holds the namespace (expired or not). Never creates the file."""
        if not self.path.is_file():
            return None
        with self._open() as conn:
            row = conn.execute(
                "SELECT run_id, expires_at FROM leases WHERE namespace = ?", (namespace,)
            ).fetchone()
        if row is None:
            return None
        return Lease(namespace, row["run_id"], _time(row["expires_at"]))

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        conn.isolation_level = None
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS leases (namespace TEXT PRIMARY KEY, "
                "run_id TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")


# --- the write switch -------------------------------------------------------------------------


def writes_disabled(path: Path) -> bool:
    """True while the write switch file exists (docs/m5-integration-plan.md, 4.3, level 2)."""
    return path.exists()


def disable_writes(path: Path, reason: str = "") -> None:
    """Switch every write off; reads keep working, so a person can still look."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{utcnow().isoformat()} {reason}".strip() + "\n", encoding="utf-8")


def enable_writes(path: Path) -> None:
    path.unlink(missing_ok=True)


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(NOT_RESET)
    return as_utc(datetime.fromisoformat(value))
