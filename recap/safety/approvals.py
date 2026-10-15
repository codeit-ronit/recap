"""Exact-scope approvals: a human "yes" for ONE proposal, stored in SQLite.

Where it sits: when the policy says APPROVAL_REQUIRED, the controller creates an
`ApprovalRequest` here and notifies an approval channel (console, API/UI, or the
simulation-only auto-approver). Whoever answers, the decision is recorded and checked
by this store, so every channel goes through the same rules:

  * an approval names one incident, one proposal and that proposal's digest
    (a fingerprint of action + target + params), so it cannot authorize anything else;
  * it expires, and it can be consumed only once (single use, enforced in SQL);
  * a decision for a different digest, a non-pending request or an expired request
    raises ApprovalError;
  * `expire()` ends a pending request early (e.g. its run was cancelled while waiting):
    the waiter returns None and a later "yes" is refused.

The gateway re-reads the approval from this store right before executing, so an
approval object held by the caller can never be stale or forged.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from recap.core.db import connect
from recap.core.errors import ApprovalError, ConfigError
from recap.core.ids import new_id, utcnow
from recap.core.models import (
    Approval,
    ApprovalRequest,
    ApprovalStatus,
    EnvironmentInfo,
    MitigationProposal,
)

Decision = Literal["approved", "rejected"]
PENDING, EXPIRED = ApprovalStatus.PENDING.value, ApprovalStatus.EXPIRED.value

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
    proposal_id     TEXT NOT NULL UNIQUE,  -- at most one decision per proposal
    proposal_digest TEXT NOT NULL,
    scope           TEXT NOT NULL,
    decision        TEXT NOT NULL,
    actor           TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);
"""
_INSERT_REQUEST = """INSERT INTO approval_requests VALUES (:proposal_id, :run_id, :incident_id,
    :proposal_digest, :status, :requested_at, :expires_at, :proposal_json)"""
_INSERT_APPROVAL = """INSERT INTO approvals VALUES (:approval_id, :incident_id, :proposal_id,
    :proposal_digest, :scope, :decision, :actor, :decided_at, :expires_at, :consumed_at)"""
_SET_STATUS_IF_PENDING = (
    "UPDATE approval_requests SET status = ? WHERE proposal_id = ? AND status = ?"
)


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
            self._write(_INSERT_REQUEST, {**row, "proposal_json": proposal.model_dump_json()})
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
        """Record a human (or auto-approver) decision on one pending request."""
        now = now or utcnow()
        request = self._load_request(proposal_id)
        if request is None:
            raise ApprovalError(f"unknown proposal {proposal_id!r}")
        if proposal_digest != request.proposal_digest:
            raise ApprovalError("approval is for a different proposal (digest mismatch)")
        if request.status != ApprovalStatus.PENDING:
            raise ApprovalError(f"approval request is {request.status.value}, not pending")
        if now >= request.expires_at:
            self.expire(proposal_id)
            raise ApprovalError("approval request expired before a decision was made")

        approval = Approval(
            approval_id=new_id("apr"),
            incident_id=request.incident_id,
            proposal_id=proposal_id,
            proposal_digest=request.proposal_digest,
            decision=decision,
            actor=actor,
            decided_at=now,
            expires_at=request.expires_at,
        )
        with self._lock, self._conn:
            # The status guard makes "decide once" hold even if two deciders race.
            moved = self._conn.execute(_SET_STATUS_IF_PENDING, (decision, proposal_id, PENDING))
            if moved.rowcount != 1:
                raise ApprovalError("approval request was decided or expired concurrently")
            self._conn.execute(_INSERT_APPROVAL, approval.model_dump(mode="json"))
        self._wake(proposal_id)
        return approval

    async def wait_for_decision(self, proposal_id: str, timeout_seconds: float) -> Approval | None:
        """Wait for a decision; None if the request expires first.

        It expires on timeout (this call then marks it EXPIRED), when its own time is up,
        or when `expire()` is called.
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
        if self._write(_SET_STATUS_IF_PENDING, (EXPIRED, proposal_id, PENDING)):
            self._wake(proposal_id)

    # --- approvals -----------------------------------------------------------------------

    def get_approval(self, approval_id: str) -> Approval | None:
        """The stored approval with this ID (the trusted copy)."""
        row = self._read_one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        return Approval.model_validate(dict(row)) if row else None

    def consume(self, approval_id: str, now: datetime | None = None) -> Approval:
        """Use an approval once. Raises ApprovalError if it is unusable or already used."""
        now = now or utcnow()
        approval = self.get_approval(approval_id)
        if approval is None:
            raise ApprovalError(f"unknown approval {approval_id!r}")
        if approval.decision != "approved":
            raise ApprovalError("a rejected approval cannot be used")
        if now >= approval.expires_at:
            raise ApprovalError("approval expired")
        # "consumed_at IS NULL" in the WHERE clause makes single use atomic in SQLite.
        used = self._write(
            "UPDATE approvals SET consumed_at = ? WHERE approval_id = ? AND consumed_at IS NULL",
            (now.isoformat(), approval_id),
        )
        if used != 1:
            raise ApprovalError(f"approval {approval_id} was already used (single use)")
        return approval.model_copy(update={"consumed_at": now})

    def approvals_for(self, incident_id: str) -> list[Approval]:
        """Every decision recorded for an incident, oldest first."""
        sql = "SELECT * FROM approvals WHERE incident_id = ? ORDER BY rowid"
        return [Approval.model_validate(dict(row)) for row in self._read(sql, (incident_id,))]

    # --- helpers -------------------------------------------------------------------------

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

    def _write(self, sql: str, params: tuple[Any, ...] | dict[str, Any]) -> int:
        """Run one statement in its own transaction; returns the number of rows changed."""
        with self._lock, self._conn:
            return self._conn.execute(sql, params).rowcount


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
