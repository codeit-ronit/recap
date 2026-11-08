"""What an interrupted run did, read from its persisted evidence, and what happens next.

A worker that stops mid-run leaves four records behind, all outside Redis: the run's last
checkpoint (RunStore), its trace up to the stop (every event, the write-ahead
`mitigation.started` included), the approval log, and the target's own audit log. assess()
reads them, never the model and never the queue, and decides:

    phase              evidence                                write     next
    before_start       no run record and no event              none      run again (same run id)
    before_write       running, no write-ahead record          none      run again (new run)
    awaiting_approval  a request still pending                 none      run again (new run); the
                                                                         request is expired
    write_in_flight    write-ahead record, no recorded outcome the target's audit log decides:
                                                               not_applied / refused: run again
                                                               applied / unknown: a person decides
    after_write        the outcome is recorded                 applied   a person decides
    verifying          the run reached VERIFYING               applied   a person decides

Why this is safe: the gateway consumes the approval and writes the write-ahead record BEFORE it
sends a write, so "no write-ahead record" proves nothing was sent; and a run that runs again is a
new run, which investigates afresh and needs a fresh approval, bound to its own proposal, for any
fix. Nothing here ever sends a write, and a write that may have reached the target is never
retried: its run is closed with `needs_reconciliation` for a person to check the target.

Two ways an attempt stops without its worker finishing the job:

    worker_lost     the worker died (killed, out of memory, power): the run never ended. Another
                    worker finds the job in the dead worker's list once its heartbeat expired.
    worker_stopped  the worker was stopped (SIGTERM past its grace period) or fenced itself off
                    (it lost Redis): it cancelled the run, which the controller ended as FAILED
                    ("run cancelled"), the write in flight awaited and recorded. The same
                    worker, or another one later, settles the job.

close_run() records the interruption inside the existing lifecycle: the run moves to FAILED (a
legal move from any running state), its record gets an Interruption (saved first: from then on
the record is final, RunStore.save), its trace gets the state change and a run.summary, and its
open approvals are voided so nothing can act for it any more. A run the controller already
ended keeps its trace as it is and only gains the Interruption. Every step can be repeated: a
settling that stops halfway is completed by the next one from the saved Interruption.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from recap.core.ids import new_id, utcnow
from recap.core.models import (
    TERMINAL_STATES,
    IncidentState,
    Interruption,
    RunRecord,
    StateChange,
    TraceEvent,
    WriteEvidence,
)
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory, Tracer
from recap.workers.queue import Job, JobQueue, JobStatus

log = logging.getLogger(__name__)

Phase = Literal[
    "before_start",
    "before_write",
    "awaiting_approval",
    "write_in_flight",
    "after_write",
    "verifying",
]
WriteState = Literal["none", "not_applied", "applied", "refused", "unknown"]
Cause = Literal["worker_lost", "worker_stopped"]

CANCELLED_NOTE = "run cancelled"  # the controller's note for a cancelled run
TERMINAL = frozenset(state.value for state in TERMINAL_STATES)
# How many events a reconciler reads back (a run has a few hundred at most).
MAX_EVENTS = 100_000


@dataclass(frozen=True)
class Assessment:
    phase: Phase
    write: WriteState
    detail: str
    started: bool  # the run had a record or events: it is closed before anything runs again

    @property
    def may_run_again(self) -> bool:
        """True only when no write can have reached the target."""
        return self.write in ("none", "not_applied", "refused")


def assess(
    record: RunRecord | None,
    events: Sequence[TraceEvent],
    *,
    pending_approval: bool,
    write_evidence: WriteEvidence | None,
) -> Assessment:
    """What the interrupted run did, from its record, its trace and the target's audit log.
    `write_evidence` is the target's own account of the run's last authorized write (None when
    the run never got that far or the target could not be asked)."""
    if record is None and not events:
        return Assessment("before_start", "none", "the run had not started", started=False)

    last_state = _stopped_in(record, events)
    started = [e for e in events if e.category == TraceCategory.MITIGATION_STARTED]
    outcomes = [e for e in events if e.category == TraceCategory.MITIGATION]

    if started:
        latest = started[-1].payload.get("invocation_id")
        outcome = next(
            (o for o in reversed(outcomes) if o.payload.get("invocation_id") == latest), None
        )
        if outcome is not None:
            applied = bool(outcome.payload.get("ok"))
            phase: Phase = (
                "verifying" if last_state == IncidentState.VERIFYING.value else "after_write"
            )
            write: WriteState = "applied" if applied else _evidence_state(write_evidence)
            said = (
                "applied"
                if applied
                else f"recorded as not applied ({outcome.payload.get('error') or 'no error given'})"
            )
            return Assessment(
                phase, write, f"the fix was sent and its outcome recorded: {said}", started=True
            )
        state = _evidence_state(write_evidence)
        why = (
            write_evidence.detail if write_evidence is not None else "the target could not be asked"
        )
        return Assessment(
            "write_in_flight",
            state,
            "the fix was sent (write-ahead record) but its outcome was never recorded; "
            f"the target's audit log: {state} ({why})",
            started=True,
        )
    if pending_approval or last_state == IncidentState.AWAITING_APPROVAL.value:
        return Assessment(
            "awaiting_approval",
            "none",
            "the run was waiting for a person's approval; no write was sent",
            started=True,
        )
    where = last_state or "its first stage"
    return Assessment(
        "before_write", "none", f"the run stopped in {where}; no write was sent", started=True
    )


def _stopped_in(record: RunRecord | None, events: Sequence[TraceEvent]) -> str:
    """The state the run was in when it stopped. The controller traces a move before it saves
    the checkpoint (IncidentController._transition), so the trace's last move is the latest
    one; a run the controller ended when its worker cancelled it is judged by the state it was
    cancelled in."""
    moves = [e.payload for e in events if e.category == TraceCategory.STATE]
    traced = str(moves[-1].get("to") or "") if moves else ""
    state = traced or (record.state.value if record is not None else "")
    if state in TERMINAL and moves and moves[-1].get("to") == state:
        return str(moves[-1].get("from") or state)
    return state


def _evidence_state(evidence: WriteEvidence | None) -> WriteState:
    """The target's own answer; "unknown" when there is none (never assumed unchanged)."""
    return evidence.state if evidence is not None else "unknown"


def write_evidence_for(
    runtime: Runtime, run_id: str, events: Sequence[TraceEvent]
) -> WriteEvidence | None:
    """Ask the target's own audit log about the run's last write-ahead record (read-only).
    Any failure to ask is "unknown": a target that cannot tell is never assumed unchanged."""
    started = [e for e in events if e.category == TraceCategory.MITIGATION_STARTED]
    if not started:
        return None
    write = started[-1].payload
    try:
        environment = runtime.environment_factory(runtime.settings.environment, run_id)
        return environment.recorded_write(
            str(write.get("approval_id") or ""), str(write.get("proposal_digest") or "")
        )
    except Exception as exc:  # fail closed
        log.warning("could not read the target's audit log for %s: %s", run_id, exc)
        return WriteEvidence(
            state="unknown", detail=f"the target's audit log could not be read: {exc}"
        )


def read_run(runtime: Runtime, run_id: str) -> tuple[RunRecord | None, list[TraceEvent]]:
    return runtime.runs.get(run_id), runtime.events.list_events(run_id, 0, MAX_EVENTS)


def run_ended(record: RunRecord | None, events: Sequence[TraceEvent]) -> bool:
    """The run reached a final state and wrote its run.summary (the controller's last word)."""
    return (
        record is not None
        and record.state in TERMINAL_STATES
        and any(event.category == TraceCategory.RUN_SUMMARY for event in events)
    )


def has_pending_approval(runtime: Runtime, run_id: str) -> bool:
    return any(request.run_id == run_id for request in runtime.approvals.pending())


def close_run(
    runtime: Runtime,
    record: RunRecord,
    events: Sequence[TraceEvent],
    assessment: Assessment,
    *,
    retried_as: str | None,
    cause: Cause,
    actor: str,
) -> Interruption:
    """Close an interrupted run from outside it: void its open approvals, save its record as
    FAILED with the Interruption, then end its trace with the state change and a run.summary
    (unless the controller already ended it). The record is saved first: from then on it is
    final (RunStore.save), and a settling that stops halfway is completed from it."""
    now = utcnow()
    needs_person = not assessment.may_run_again
    voided = runtime.approvals.void_for_run(
        record.run_id, f"the worker running this incident stopped ({actor} closed the run)"
    )
    note = _note(assessment, retried_as, needs_person)
    interruption = Interruption(
        at=now,
        phase=assessment.phase,
        write=assessment.write,
        needs_reconciliation=needs_person,
        detail=assessment.detail + (f"; voided: {', '.join(voided)}" if voided else ""),
        retried_as=retried_as,
        cause=cause,
    )
    if not run_ended(record, events):
        if record.state not in TERMINAL_STATES:
            record.history.append(StateChange(state=IncidentState.FAILED, at=now, reason=note))
            record.state = IncidentState.FAILED
        record.outcome_note = note
        record.error = f"interrupted: {assessment.detail}"
    # else: the controller ended the run when its worker cancelled it; its note stays.
    record.pending_approval = None
    record.interruption = interruption
    record.updated_at = now
    runtime.runs.save(record)
    finish_trace(runtime, record, events)
    return interruption


def finish_trace(runtime: Runtime, record: RunRecord, events: Sequence[TraceEvent]) -> None:
    """End a closed run's trace as the controller would have: the move to FAILED (when it was
    not recorded) and a run.summary carrying the interruption. Nothing when it has a summary."""
    if record.interruption is None or any(e.category == TraceCategory.RUN_SUMMARY for e in events):
        return
    last_seq = max((event.seq for event in events), default=0)
    tracer = Tracer(record.run_id, [runtime.events, runtime.jsonl], start_seq=last_seq)
    moved = any(
        e.category == TraceCategory.STATE and e.payload.get("to") == record.state.value
        for e in events
    )
    if not moved:
        tracer.stage = record.state.value
        origin = _stopped_in(None, events) or None  # the last state the trace holds
        last = record.history[-1] if record.history else None
        tracer.emit(
            TraceCategory.STATE,
            {"from": origin, "to": record.state.value, "reason": record.outcome_note},
            # the time close_run recorded for the move, as the controller's moves do
            ts=last.at if last is not None and last.state == record.state else None,
        )
    tracer.emit(
        TraceCategory.RUN_SUMMARY,
        {
            "state": record.state.value,
            "outcome_note": record.outcome_note,
            "recovered": False,
            "interrupted": record.interruption.model_dump(mode="json"),
            "simulated": bool(record.environment and record.environment.is_simulated),
        },
    )


def _note(assessment: Assessment, retried_as: str | None, needs_person: bool) -> str:
    phase = assessment.phase.replace("_", " ")
    if needs_person:
        return (
            f"interrupted ({phase}): the fix may have reached the target "
            f"(write {assessment.write}); not retried, a person must check the target"
        )
    again = f"; retried as {retried_as}" if retried_as else "; not retried"
    return f"interrupted ({phase}): no fix reached the target{again}"


# --- jobs --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Decision:
    retry: bool
    next_run: str | None  # a retry's new run (None: the same run id, it never started)
    may_run_again: bool
    phase: Phase
    write: WriteState
    detail: str
    cause: Cause


def reconcile_job(
    runtime: Runtime, queue: JobQueue, job_id: str, held_by: str, *, actor: str
) -> str:
    """Decide, from the run's persisted evidence, what happens to a job whose attempt stopped
    without its worker finishing it. The caller holds the job's lock (JobQueue.lock).
    Returns what was done, in one line (for logs and tests).

    Every step can be repeated: a settling that stops halfway (its process died too) is
    completed by the next one, from the run's saved Interruption, never decided twice."""
    if job_id not in queue.held_by(held_by):
        return f"{job_id}: no longer held by {held_by}; already settled"
    job = queue.get(job_id)
    if job is None or job.terminal:
        queue.release(job_id, held_by)
        return f"{job_id}: {'no such job' if job is None else job.status}; dropped from {held_by}"
    record, events = read_run(runtime, job.run_id)
    if record is not None and record.interruption is not None:
        # Closed by an earlier settling that stopped before the job's bookkeeping.
        finish_trace(runtime, record, events)
        found = record.interruption
        decision = _Decision(
            retry=found.retried_as is not None and not job.cancel_requested,
            next_run=found.retried_as,
            may_run_again=not found.needs_reconciliation,
            phase=found.phase,
            write=found.write,
            detail=found.detail,
            cause=found.cause or "worker_lost",
        )
    else:
        # A final state in the record means the run ended: the record is the source of truth,
        # even when its process died before the trace's run.summary (memory consolidation
        # comes in between).
        ended = record is not None and record.state in TERMINAL_STATES
        cancelled_run = record is not None and ended and record.outcome_note == CANCELLED_NOTE
        if record is not None and ended and (job.cancel_requested or not cancelled_run):
            # The run ended by itself, or as a person asked: only the job's bookkeeping was lost.
            status: JobStatus = "cancelled" if cancelled_run else "completed"
            queue.finish(
                job, held_by, status=status, category=record.state.value, detail=record.outcome_note
            )
            return f"{job_id}: its run {job.run_id} had already ended ({record.state.value})"
        cause: Cause = "worker_stopped" if ended else "worker_lost"
        evidence = write_evidence_for(runtime, job.run_id, events)
        pending = has_pending_approval(runtime, job.run_id)
        verdict = assess(record, events, pending_approval=pending, write_evidence=evidence)
        retry = (
            verdict.may_run_again and not job.cancel_requested and job.attempt < job.max_attempts
        )
        decision = _Decision(
            retry=retry,
            next_run=new_id("run") if retry and verdict.started else None,
            may_run_again=verdict.may_run_again,
            phase=verdict.phase,
            write=verdict.write,
            detail=verdict.detail,
            cause=cause,
        )
        if record is not None:
            close_run(
                runtime,
                record,
                events,
                verdict,
                retried_as=decision.next_run,
                cause=cause,
                actor=actor,
            )
        elif verdict.started:  # events without a record: void whatever it asked for
            runtime.approvals.void_for_run(
                job.run_id, f"the worker running this incident stopped ({actor})"
            )
    return _apply(queue, job, held_by, decision)


def _apply(queue: JobQueue, job: Job, held_by: str, decision: _Decision) -> str:
    """The job's bookkeeping for a decision (Redis only; the run was closed before)."""
    category = f"{decision.cause}_{decision.phase}"
    said = f"{job.job_id}: {decision.cause} {decision.phase}"
    if decision.retry:
        if decision.next_run is not None:
            queue.set_run(job, decision.next_run)
        queue.requeue(job, held_by, category=category, detail=decision.detail)
        return f"{said}; runs again as {job.run_id}"
    if job.cancel_requested:
        queue.finish(job, held_by, status="cancelled", category=category, detail=decision.detail)
        return f"{said}; cancelled, as a person asked"
    if not decision.may_run_again:
        queue.finish(
            job, held_by, status="needs_reconciliation", category=category, detail=decision.detail
        )
        return f"{said}, write {decision.write}; a person checks the target"
    queue.finish(job, held_by, status="failed", category="max_attempts", detail=decision.detail)
    return f"{said}; no attempt left ({job.attempt} of {job.max_attempts})"


def settle(
    runtime: Runtime, queue: JobQueue, job_id: str, held_by: str, *, actor: str
) -> str | None:
    """reconcile_job under the job's lock; None when another process holds the lock."""
    if not queue.lock(job_id, actor):
        return None
    try:
        return reconcile_job(runtime, queue, job_id, held_by, actor=actor)
    finally:
        queue.unlock(job_id)


def recover_dead_workers(runtime: Runtime, queue: JobQueue, actor: str) -> list[str]:
    """For every worker whose heartbeat expired, settle each job it still held."""
    done: list[str] = []
    for worker_id, heartbeat in queue.workers().items():
        if heartbeat is not None or worker_id == actor:
            continue
        for job_id in queue.held_by(worker_id):
            outcome = settle(runtime, queue, job_id, worker_id, actor=actor)
            if outcome is not None:
                log.info("recovered %s", outcome)
                done.append(outcome)
        queue.forget(worker_id)
    return done
