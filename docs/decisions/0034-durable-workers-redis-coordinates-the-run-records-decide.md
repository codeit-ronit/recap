# ADR 0034: Durable workers: Redis coordinates, the run records decide, and an uncertain write is never retried

**In one sentence:** With `RECAP_EXECUTION_BACKEND=redis`, the API queues incidents in Redis
and worker processes execute them through the unchanged harness; when a worker dies, another
settles its job from the run's persisted evidence (the write-ahead record, the approval log and
the target's own audit log), and runs it again, as a new run with a fresh approval, only when
no write can have reached the target.

## Status

Accepted (M6, W3; built 2026-11-08). Builds on [ADR 0027](0027-a-typed-read-only-api-over-the-run-records.md)
(the typed run views) and on the write path's ordering ([ADR 0032](0032-a-real-kubernetes-target-behind-the-same-interface.md):
approval used, write-ahead record, write, outcome). The in-process backend stays the default.
The work is logged in [m6-progress.md](../m6-progress.md).

## Context

Until M6 an incident ran as an asyncio task inside the API process. A restart lost every
running incident (its record stayed at its last checkpoint, never closed), a burst of
submissions had no bound, and nothing could run incidents on another machine. A queue fixes the
scheduling; the hard part is the crash: a worker can die before it claims, while it
investigates, while it waits for a person, right before it sends an approved write, right after
the target applied it, or while it verifies the recovery. Replaying a Kubernetes mutation
because a process died would be the worst possible outcome; so would a retry that reuses an
approval a person gave for a different moment.

Exactly-once execution is not achievable across a crash. The decision is what to promise
instead.

## Decision

1. **Redis only coordinates** (`recap/workers/queue.py`, a 20-command RESP client in
   `recap/workers/resp.py`, no new dependency). Submission is idempotent (an `Idempotency-Key`
   names one job for a day; the same key with another request is refused) and bounded (a full
   queue answers 429). Claiming is atomic (`BLMOVE` into the worker's own processing list).
   Every multi-key change is one `MULTI/EXEC`. A job's status says what happened to the job,
   never whether the incident was resolved: that is the run record's to say. Nothing read from
   Redis is trusted: a worker checks every job again exactly as the API checks a request, so a
   job written to Redis some other way runs only what the API would have started.
2. **At-least-once delivery, with safe reconciliation.** A worker refreshes a heartbeat key with
   a TTL; one silent past it is dead, and another worker settles each job it held, under a lock,
   from four records outside Redis (`recap/workers/reconcile.py`): the run's checkpoint, its
   trace (the controller traces a move before it checkpoints, so the trace is the latest word),
   the approval log, and the target's own audit log (`TargetEnvironment.recorded_write`).
3. **A write that may have landed is never retried.** "No write-ahead record" proves nothing was
   sent (the gateway writes it before sending). With one and no recorded outcome, only the
   target's audit log can say "not applied": the simulator's action log can (it is written in the
   write's own transaction); a real cluster's audit row is written after the cluster answers, so
   its absence proves nothing, and the answer is `unknown`. Applied or unknown: the job ends as
   `needs_reconciliation` and a person checks the target.
4. **A retry is a new run that asks again.** A run that may run again is closed as FAILED with an
   `Interruption` (phase, write state, cause, the run it continued as), its pending requests are
   expired and its unused approvals revoked (`ApprovalStore.void_for_run`), and the job runs
   again under a new run id (the same id only when nothing had started): a fresh investigation
   and a fresh approval bound to its own proposal. A used approval stays used. Attempts are
   capped (`RECAP_JOB_MAX_ATTEMPTS`).
5. **Fencing, and a closed run is final.** A worker that cannot refresh its heartbeat before
   the TTL would run out, or finds it expired, cancels its own incidents (the controller ends
   them; a write in flight is awaited and recorded by the gateway). `RunStore.save` ignores a
   checkpoint without an interruption over one with it, so a process presumed dead cannot undo
   the closing. Every settling step can be repeated: one that stops halfway is completed from
   the saved interruption.
6. **The same contracts, and the stream from SQLite.** `POST /incidents/run` answers `queued`
   with the job's id and position; run statuses gain `queued`, `interrupted` and `cancelled`, run
   outcomes `interrupted` and `needs_reconciliation`; `GET /queue` and `GET /jobs/{id}` are
   read-only. A worker's run streams from its stored trace (`id` = seq, so `Last-Event-ID`
   resumes it), so a worker or API restart loses no event. Mission Control gets a notice on the
   live run page, not a redesign.
7. **Optional, and packaged without being required.** RECAP starts and runs without Redis; the
   Redis backend is chosen explicitly. `infra/compose/` packages the API, the dashboard, Redis,
   workers and an optional Prometheus (localhost-only ports, non-root, no capabilities, read-only
   root filesystems, health-gated startup); it was written and checked offline, not started.

## Consequences

### Good

- An API restart loses nothing; a worker crash at any of seven points ends in a closed,
  explained run and either a safe retry or a clear request for a person, proven by tests that
  kill real worker processes with SIGKILL at each point and assert at most one applied write.
- Backpressure, idempotency and cancellation are part of the contract, and the queue is
  observable (`GET /queue`, `/metrics`).

### Bad

- At-least-once means a job can be attempted more than once; only a run that provably sent
  nothing is retried, and the cost is that some crashes end in `needs_reconciliation`.
- Fencing relies on timing; the guarantees against duplicate writes do not (single-use
  approvals, capability nonces, the namespace lease on live targets).
- Run-event metrics (tool calls, tokens) are counted per process; workers expose none yet.
- Redis without persistence (no append-only file) loses queued jobs on a restart, never runs.

## Alternatives considered

- **Celery, RQ or arq.** Each adds a dependency and its own retry semantics, which would replay
  a task after a crash: exactly what a write path must not do. The queue RECAP needs is small.
- **Resume a run from its checkpoint.** Rejected: the model's context, the evidence registry
  and an approval bound to a moment cannot be restored faithfully; a fresh run is honest.
- **Retry on a timer when the outcome is unknown.** Rejected: a real cluster may have applied
  the write; only a person who checks the target may decide.
- **Redis Streams with consumer groups.** Comparable guarantees, more moving parts (pending
  entry lists, claims by idle time) than lists, a lease key and a lock.
