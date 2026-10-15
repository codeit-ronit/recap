"""Exact-scope approvals: a human "yes" for ONE proposal, stored in SQLite.

Where it sits: when the policy says APPROVAL_REQUIRED, the controller creates an
`ApprovalRequest` here and notifies an approval channel (console, API/UI, or the
simulation-only auto-approver). Whoever answers, the decision is recorded and checked
by this store, so every channel goes through the same rules:

  * an approval names one incident, one proposal and that proposal's digest
    (a fingerprint of action + target + params), so it cannot authorize anything else;
  * it expires, and it can be consumed only once (single use);
  * an explicit "no" wins until the approval is used: a rejection is accepted while the
    request is pending AND after an approval that was not used yet (a revocation), and
    after a rejection no approval is accepted any more (audit finding 2.4: V1 let a
    separate API approval override the operator's "n"). The gateway uses (consumes) an
    approval immediately before it sends the write, so from then on "no" comes too late;
  * `expire()` ends a pending request early (e.g. its run was cancelled while waiting):
    the waiter returns None and a later "yes" is refused.

The state machine of one request:

    pending --approve--> approved --consume (the gateway, right before the write)--> approved,
       |                    |                                                    consumed_at set
       +--reject-->  rejected  <--reject (revocation; refused once consumed_at is set)
       |
       +--expire / timeout--> expired

Every transition is a compare-and-swap inside ONE SQLite write transaction (BEGIN
IMMEDIATE takes the write lock before anything is read), so two processes that share the
database file (the API server and the CLI, say) are serialized and can never both win.
Every step, including each refused attempt, is appended to `approval_events`, a decision
log in which the database itself refuses updates, deletes and replacements (triggers: a guard
against bugs and accidental edits, not against someone who can write the file).

The gateway re-reads the approval from this store right before executing, so an
approval object held by the caller can never be stale or forged.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from recap.core.db import connect
from recap.core.errors import ApprovalError, ConfigError
from recap.core.ids import new_id, utcnow
from recap.core.models import (
    Approval,
    ApprovalEvent,
    ApprovalEventKind,
    ApprovalRequest,
    ApprovalStatus,
    EnvironmentInfo,
    MitigationProposal,
)

Decision = Literal["approved", "rejected"]
PENDING, EXPIRED = ApprovalStatus.PENDING.value, ApprovalStatus.EXPIRED.value
APPROVED, REJECTED = ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value
SYSTEM_ACTOR = "recap"  # the store or the controller itself (expiry, single use)

# A waiter also re-reads the database this often, so a decision written by another
# process (e.g. the API server) is noticed even without an in-process wake-up.
_POLL_SECONDS = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    proposal_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    incident_id     TEXT NOT NULL,
    proposal_digest TEXT NOT NULL,
    status          TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    proposal_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id     TEXT PRIMARY KEY,
    incident_id     TEXT NOT NULL,
    proposal_id     TEXT NOT NULL UNIQUE,  -- the current decision for a proposal: at most one
    proposal_digest TEXT NOT NULL,
    scope           TEXT NOT NULL,
    decision        TEXT NOT NULL,
    actor           TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);
CREATE TABLE IF NOT EXISTS approval_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    actor           TEXT NOT NULL,
    at              TEXT NOT NULL,
    proposal_digest TEXT NOT NULL,
    detail          TEXT NOT NULL
);
-- The decision log is append-only: the database refuses to change, delete or replace an
-- entry. This guards against bugs and accidental edits; someone who can write the file could
-- still drop these triggers (files under data/ are trusted state).
CREATE TRIGGER IF NOT EXISTS approval_events_no_update BEFORE UPDATE ON approval_events
    BEGIN SELECT RAISE(ABORT, 'approval events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS approval_events_no_delete BEFORE DELETE ON approval_events
    BEGIN SELECT RAISE(ABORT, 'approval events are append-only'); END;
-- INSERT OR REPLACE removes the old row without firing delete triggers, so refuse it here.
CREATE TRIGGER IF NOT EXISTS approval_events_no_replace BEFORE INSERT ON approval_events
    WHEN EXISTS (SELECT 1 FROM approval_events WHERE event_id = NEW.event_id)
    BEGIN SELECT RAISE(ABORT, 'approval events are append-only'); END;
"""
_INSERT_REQUEST = """INSERT INTO approval_requests VALUES (:proposal_id, :run_id, :incident_id,
    :proposal_digest, :status, :requested_at, :expires_at, :proposal_json)"""
_INSERT_APPROVAL = """INSERT INTO approvals VALUES (:approval_id, :incident_id, :proposal_id,
    :proposal_digest, :scope, :decision, :actor, :decided_at, :expires_at, :consumed_at)"""
_INSERT_EVENT = """INSERT INTO approval_events (proposal_id, kind, actor, at, proposal_digest,
    detail) VALUES (?, ?, ?, ?, ?, ?)"""
# Compare-and-swap: move a request from one status to another, or do nothing.
_MOVE_STATUS = "UPDATE approval_requests SET status = ? WHERE proposal_id = ? AND status = ?"


class ApprovalStore:
    """Records approval requests and decisions, and enforces their binding rules."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._conn = _open(db_path)
        # One connection is shared, and the API may call decide() from a worker thread.
        self._lock = threading.Lock()
        self._waiters: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # --- requests ------------------------------------------------------------------------

    def create_request(
        self,
        proposal: MitigationProposal,
        run_id: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Open a pending request for one exact proposal."""
        now = now or utcnow()
        request = ApprovalRequest(
            proposal_id=proposal.proposal_id,
            run_id=run_id,
            incident_id=proposal.incident_id,
            proposal_digest=proposal.digest(),
            proposal=proposal,
            requested_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        row = request.model_dump(mode="json", exclude={"proposal"})
        try:
            with self._transaction() as conn:
                conn.execute(_INSERT_REQUEST, {**row, "proposal_json": proposal.model_dump_json()})
                detail = f"run {run_id}; expires {request.expires_at.isoformat()}"
                _event(conn, request.proposal_id, "requested", SYSTEM_ACTOR, now, row, detail)
        except sqlite3.IntegrityError as exc:
            raise ApprovalError(f"a request for proposal {proposal.proposal_id} exists") from exc
        return request

    def get_request(self, proposal_id: str) -> ApprovalRequest | None:
        """The request for a proposal (marked EXPIRED if its time is up)."""
        request = self._load_request(proposal_id)
        if request and request.status == ApprovalStatus.PENDING and utcnow() >= request.expires_at:
            self.expire(proposal_id)
            request = self._load_request(proposal_id)
        return request

    def pending(self) -> list[ApprovalRequest]:
        """Requests still waiting for a decision, oldest first."""
        rows = self._read(
            "SELECT proposal_id FROM approval_requests WHERE status = ? ORDER BY rowid", (PENDING,)
        )
        requests = [self.get_request(row["proposal_id"]) for row in rows]
        return [r for r in requests if r is not None and r.status == ApprovalStatus.PENDING]

    # --- decisions -----------------------------------------------------------------------

    def decide(
        self,
        proposal_id: str,
        *,
        decision: Decision,
        proposal_digest: str,
        actor: str,
        now: datetime | None = None,
    ) -> Approval:
        """Record a human (or auto-approver) decision on one request.

        Approve: only a pending request. Reject: a pending request, or an approved one whose
        approval was not used yet (a revocation: "no" wins until the approval is used). Anything
        else raises ApprovalError, and the refused attempt is still written to the log.
        """
        now = now or utcnow()
        with self._transaction() as conn:
            outcome = self._decide(conn, proposal_id, decision, proposal_digest, actor, now)
        if isinstance(outcome, str):
            raise ApprovalError(outcome)
        self._wake(proposal_id)
        return outcome

    def _decide(
        self,
        conn: sqlite3.Connection,
        proposal_id: str,
        decision: Decision,
        digest: str,
        actor: str,
        now: datetime,
    ) -> Approval | str:
        """The decision inside the write transaction: an Approval, or the refusal reason."""
        attempt = {"proposal_digest": digest}
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return _refuse(
                conn, proposal_id, actor, now, attempt, f"unknown proposal {proposal_id!r}"
            )
        if digest != row["proposal_digest"]:
            reason = "approval is for a different proposal (digest mismatch)"
            return _refuse(conn, proposal_id, actor, now, attempt, reason)
        status = row["status"]
        if status == PENDING and now >= datetime.fromisoformat(row["expires_at"]):
            conn.execute(_MOVE_STATUS, (EXPIRED, proposal_id, PENDING))
            _event(conn, proposal_id, "expired", SYSTEM_ACTOR, now, attempt, "no decision in time")
            reason = "approval request expired before a decision was made"
            return _refuse(conn, proposal_id, actor, now, attempt, reason)
        if status == PENDING:
            return self._record(conn, row, decision, actor, now)
        if status == APPROVED and decision == "rejected":
            return self._revoke(conn, row, actor, now)
        return _refuse(
            conn, proposal_id, actor, now, attempt, f"approval request is {status}, not pending"
        )

    def _record(
        self,
        conn: sqlite3.Connection,
        request: sqlite3.Row,
        decision: Decision,
        actor: str,
        now: datetime,
    ) -> Approval:
        """pending -> approved | rejected, with the decision row and its log entry."""
        approval = Approval(
            approval_id=new_id("apr"),
            incident_id=request["incident_id"],
            proposal_id=request["proposal_id"],
            proposal_digest=request["proposal_digest"],
            decision=decision,
            actor=actor,
            decided_at=now,
            expires_at=datetime.fromisoformat(request["expires_at"]),
        )
        # We hold the write lock, so the swap cannot fail; checking keeps the rule explicit.
        moved = conn.execute(_MOVE_STATUS, (decision, approval.proposal_id, PENDING))
        if moved.rowcount != 1:  # pragma: no cover - impossible under BEGIN IMMEDIATE
            raise ApprovalError("approval request was decided or expired concurrently")
        conn.execute(_INSERT_APPROVAL, approval.model_dump(mode="json"))
        _event(conn, approval.proposal_id, decision, actor, now, request, "")
        return approval

    def _revoke(
        self, conn: sqlite3.Connection, request: sqlite3.Row, actor: str, now: datetime
    ) -> Approval | str:
        """approved -> rejected while the approval is unused: an explicit "no" wins."""
        proposal_id = request["proposal_id"]
        current = conn.execute(
            "SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if current is None or current["consumed_at"] is not None:
            reason = (
                "the approval was already used to send the action; "
                "a rejection can no longer stop it"
            )
            return _refuse(conn, proposal_id, actor, now, request, reason)
        conn.execute(_MOVE_STATUS, (REJECTED, proposal_id, APPROVED))
        conn.execute(
            "UPDATE approvals SET decision = ?, actor = ?, decided_at = ? "
            "WHERE proposal_id = ? AND consumed_at IS NULL",
            (REJECTED, actor, now.isoformat(), proposal_id),
        )
        detail = f"overrides the approval by {current['actor']} before it was used"
        _event(conn, proposal_id, "revoked", actor, now, request, detail)
        approval = Approval.model_validate(dict(current))
        return approval.model_copy(
            update={"decision": "rejected", "actor": actor, "decided_at": now}
        )

    async def wait_for_decision(self, proposal_id: str, timeout_seconds: float) -> Approval | None:
        """Wait for a decision; None if the request expires first.

        It expires on timeout (this call then marks it EXPIRED), when its own time is up,
        or when `expire()` is called. The returned approval is the store's current decision,
        so a revocation that happened before the wait ended is already in it.
        """
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        self._waiters[proposal_id] = (loop, event)
        deadline = loop.time() + timeout_seconds
        try:
            while True:
                event.clear()  # clear BEFORE reading, so a decision made after the read wakes us
                approval = self._approval_for(proposal_id)
                if approval is not None:
                    return approval
                request = self.get_request(proposal_id)
                if request is None:
                    raise ApprovalError(f"unknown proposal {proposal_id!r}")
                remaining = deadline - loop.time()
                if request.status == ApprovalStatus.EXPIRED or remaining <= 0:
                    self.expire(proposal_id)
                    return self._approval_for(proposal_id)  # a last-moment decision still counts
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, _POLL_SECONDS))
        finally:
            self._waiters.pop(proposal_id, None)

    def expire(self, proposal_id: str) -> None:
        """Mark a still-pending request EXPIRED and wake its waiter; a no-op otherwise.

        The controller calls this when a run is cancelled while it waits, so nobody can
        approve the request afterwards. The status guard keeps a decision that was already
        recorded, even one that races with this call.
        """
        now = utcnow()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            moved = conn.execute(_MOVE_STATUS, (EXPIRED, proposal_id, PENDING)).rowcount
            if moved:
                _event(conn, proposal_id, "expired", SYSTEM_ACTOR, now, row, "no decision in time")
        if moved:
            self._wake(proposal_id)

    # --- approvals -----------------------------------------------------------------------

    def get_approval(self, approval_id: str) -> Approval | None:
        """The stored approval with this ID (the trusted copy)."""
        row = self._read_one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        return Approval.model_validate(dict(row)) if row else None

    def consume(self, approval_id: str, now: datetime | None = None) -> Approval:
        """Use an approval once, right before the write. Raises ApprovalError if unusable.

        Checked and marked in one transaction: the approval must still be the current
        "approved" decision of an approved request, unexpired and unused.
        """
        now = now or utcnow()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            reason = _unusable(conn, row, approval_id, now)
            if reason is not None:
                if row is not None:  # an unknown ID has no proposal to log against
                    _refuse(conn, row["proposal_id"], SYSTEM_ACTOR, now, row, reason)
            else:
                conn.execute(
                    "UPDATE approvals SET consumed_at = ? WHERE approval_id = ? "
                    "AND consumed_at IS NULL",
                    (now.isoformat(), approval_id),
                )
                _event(conn, row["proposal_id"], "consumed", SYSTEM_ACTOR, now, row, "")
        if reason is not None:
            raise ApprovalError(reason)
        return Approval.model_validate(dict(row)).model_copy(update={"consumed_at": now})

    def approvals_for(self, incident_id: str) -> list[Approval]:
        """Every decision recorded for an incident, oldest first."""
        sql = "SELECT * FROM approvals WHERE incident_id = ? ORDER BY rowid"
        return [Approval.model_validate(dict(row)) for row in self._read(sql, (incident_id,))]

    # --- the decision log ------------------------------------------------------------------

    def events(self, proposal_id: str) -> list[ApprovalEvent]:
        """The full decision log of one proposal, oldest first."""
        sql = "SELECT * FROM approval_events WHERE proposal_id = ? ORDER BY event_id"
        return [ApprovalEvent.model_validate(dict(row)) for row in self._read(sql, (proposal_id,))]

    def recent_events(self, limit: int = 50) -> list[ApprovalEvent]:
        """The newest `limit` log entries across all proposals, newest first."""
        sql = "SELECT * FROM approval_events ORDER BY event_id DESC LIMIT ?"
        return [ApprovalEvent.model_validate(dict(row)) for row in self._read(sql, (limit,))]

    # --- helpers -------------------------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction that takes SQLite's write lock BEFORE reading anything.

        That is what turns every read-check-write below into a compare-and-swap that holds
        across processes: a second process waits (up to the busy timeout) until we commit.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _load_request(self, proposal_id: str) -> ApprovalRequest | None:
        row = self._read_one(
            "SELECT * FROM approval_requests WHERE proposal_id = ?", (proposal_id,)
        )
        if row is None:
            return None
        data = dict(row)
        data["proposal"] = MitigationProposal.model_validate_json(data.pop("proposal_json"))
        return ApprovalRequest.model_validate(data)

    def _approval_for(self, proposal_id: str) -> Approval | None:
        row = self._read_one("SELECT * FROM approvals WHERE proposal_id = ?", (proposal_id,))
        return Approval.model_validate(dict(row)) if row else None

    def _wake(self, proposal_id: str) -> None:
        """Wake an in-process waiter (thread-safe: decide() may run in a web worker thread)."""
        waiter = self._waiters.get(proposal_id)
        if waiter is not None and not waiter[0].is_closed():
            loop, event = waiter
            loop.call_soon_threadsafe(event.set)

    def _read(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _read_one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        rows = self._read(sql, params)
        return rows[0] if rows else None


def _unusable(
    conn: sqlite3.Connection, row: sqlite3.Row | None, approval_id: str, now: datetime
) -> str | None:
    """Why an approval cannot be used right now, or None if it can (inside the transaction)."""
    if row is None:
        return f"unknown approval {approval_id!r}"
    if row["decision"] != APPROVED:
        return "a rejected approval cannot be used"
    if row["consumed_at"] is not None:
        return f"approval {approval_id} was already used (single use)"
    if now >= datetime.fromisoformat(row["expires_at"]):
        return "approval expired"
    request = conn.execute(
        "SELECT status FROM approval_requests WHERE proposal_id = ?", (row["proposal_id"],)
    ).fetchone()
    if request is None or request["status"] != APPROVED:
        status = request["status"] if request else "missing"
        return f"the approval request is {status}, so the approval cannot be used"
    return None


def _event(
    conn: sqlite3.Connection,
    proposal_id: str,
    kind: ApprovalEventKind,
    actor: str,
    at: datetime,
    source: sqlite3.Row | dict[str, Any] | None,
    detail: str,
) -> None:
    """Append one entry to the decision log (inside the caller's transaction)."""
    digest = str(source["proposal_digest"]) if source is not None else ""
    conn.execute(_INSERT_EVENT, (proposal_id, kind, actor, at.isoformat(), digest, detail))


def _refuse(
    conn: sqlite3.Connection,
    proposal_id: str,
    actor: str,
    at: datetime,
    source: sqlite3.Row | dict[str, Any] | None,
    reason: str,
) -> str:
    """Log a refused attempt and return its reason (the caller raises after committing)."""
    _event(conn, proposal_id, "refused", actor, at, source, reason)
    return reason


def _open(db_path: Path | str | None) -> sqlite3.Connection:
    """In-memory database for tests and demos, or a file via the shared helper."""
    if db_path is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    return connect(db_path)


# --- approval channels (who answers a request) ----------------------------------------


class AutoApprovalChannel:
    """Approves every request automatically. Refuses to exist outside a simulation."""

    name = "auto-approver (simulation only)"

    def __init__(self, store: ApprovalStore, environment: EnvironmentInfo) -> None:
        if not environment.is_simulated:
            raise ConfigError("the auto-approver may only be used in a simulated environment")
        self._store = store

    async def notify(self, request: ApprovalRequest) -> None:
        """Approve exactly the proposal in the request (same digest checks as a human)."""
        self._store.decide(
            request.proposal_id,
            decision="approved",
            proposal_digest=request.proposal_digest,
            actor="auto-approver[simulation]",
        )


class ConsoleApprovalChannel:
    """Asks the person at the terminal to approve or reject one exact action."""

    name = "console"

    def __init__(
        self,
        store: ApprovalStore,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], object] = print,
    ) -> None:
        self._store = store
        self._input = input_fn
        self._output = output_fn

    async def notify(self, request: ApprovalRequest) -> None:
        """Show the proposal, ask y/N, and record the answer (anything but y/yes rejects)."""
        for line in _describe(request):
            self._output(line)
        try:
            # input() blocks, so it runs in a thread and the event loop keeps running.
            answer = await asyncio.to_thread(self._input, "Approve this exact action? [y/N] ")
        except EOFError:  # no terminal attached: treat as "no"
            answer = ""
        approved = str(answer).strip().lower() in {"y", "yes"}
        self._store.decide(
            request.proposal_id,
            decision="approved" if approved else "rejected",
            proposal_digest=request.proposal_digest,
            actor="console-user",
        )


class ExternalApprovalChannel:
    """A human decides in the dashboard; the API calls ApprovalStore.decide()."""

    name = "external (API/UI)"

    async def notify(self, request: ApprovalRequest) -> None:
        """Nothing to send: the pending request is already visible through the API."""


def _describe(request: ApprovalRequest) -> list[str]:
    """Human-readable summary of exactly what would be executed."""
    proposal = request.proposal
    return [
        f"Approval required for proposal {request.proposal_id} (incident {request.incident_id})",
        f"  action:    {_printable(proposal.action)}",
        f"  target:    {_printable(proposal.target)}",
        f"  params:    {_printable(str(proposal.params))}",
        f"  rationale: {_printable(proposal.rationale)}  [planner text, untrusted]",
        f"  evidence:  {_printable(', '.join(proposal.evidence_ids) or 'none')}",
        f"  digest:    {request.proposal_digest[:12]}",
        f"  expires:   {request.expires_at.isoformat()}",
    ]


def _printable(text: str, limit: int = 300) -> str:
    """Drop control characters (e.g. terminal escape codes in model text) and cap the length."""
    cleaned = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."
