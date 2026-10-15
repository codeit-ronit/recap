# ADR 0013: Wait for live targets to settle, judge each epoch on its own evidence, bound every provider call

**In one sentence:** after a fix the controller waits a bounded time for a live target to
settle before the oracle's verdict counts, the verifier ignores the fixed service's state
observations from before the fix, and no provider call may outlive the run's wall-clock
budget.

## Status

Accepted (M1, 2026-10-15). Fixes audit finding 2.6.

## Context

Three gaps in V1's recovery lifecycle:

- **No stabilization wait.** The oracle was probed once, right after the write. The
  simulator recovers instantly (its clock jumps 5 minutes per action), but a real rollout
  needs time; the right fix would be judged "not recovered".
- **Stale evidence after a half-working fix.** After restarting `payment` in a two-fault
  scenario, the next round still saw the OOM kills from before the restart, blamed
  `payment` again, and gave up. No test reached RESOLVED on a second fix.
- **Unbounded provider calls.** An agent's timeout counted from its own start, so a provider
  call that hung past the run's budget ended the run FAILED (or, in the proposal stage,
  ran on with no deadline at all).

## Decision

- `EnvironmentInfo` gets `stabilization_timeout_seconds` (default 60) and
  `stabilization_poll_seconds` (default 2). The controller's `_await_recovery` probes the
  oracle until it reports healthy, the window ends, or the run's time runs out. A simulated
  target is probed once (the simulator also declares a window of 0). The trace records the
  number of probes and the time waited.
- The controller records when every executed fix began: the environment time of the
  oracle's own "before" probe, a trusted reading taken just before the write (never a time
  the tool reports about itself). It passes `remediated` to `Verifier.verify`. For a fixed
  service only newer state observations count: OOM kills, memory, health snapshots,
  error-rate and latency points, and its own log lines. Blame that other services put on a
  fixed service still counts until fresh platform evidence shows it healthy. History still
  counts: deployments and when errors first began. A release that a later deployment
  replaced cannot explain errors now.
- `BudgetTracker.remaining_seconds()`. The orchestrator gives each agent the shorter of its
  own limit and the time left; when the run's clock is what ran out, the outcome is
  `BudgetExhausted` (a clean BUDGET_EXHAUSTED stop), not an agent failure. The proposal call
  is bounded the same way.
- Retries stay bounded by `max_mitigation_attempts`, and an action already tried on a
  target is never proposed again.

## Consequences

### Good

- All four remaining AUDIT-2.6 reproductions pass: a half-working restart is followed by a
  separately investigated rollback (RESOLVED, two exact single-use approvals); a live-like
  target that needs 1.5 s to settle is RESOLVED after the wait; a hanging provider ends
  BUDGET_EXHAUSTED at the deadline in both stages.
- Simulated runs are unchanged (one probe, no waiting).

### Bad

- A live run can take up to `stabilization_timeout_seconds` longer per fix, and that time
  counts against the run's wall-clock budget.
- The epoch rule is per service. A fix that changes a *different* service's state (a
  dependency fix that heals its callers) does not mark the callers' older observations as
  stale.
- The waiting rule is "healthy or time is up"; it does not watch a rollout's own progress
  (for example Kubernetes rollout status). That belongs in a real adapter (M5).

## Amended after the M1 review

Three independent reviews (security, correctness, test quality) of the M1 fixes found edge
cases in this lifecycle. Each is fixed, and each fix has a test that fails without it:

- **The epoch boundary is a trusted time.** It used to be the `executed_at` that the target
  reported about itself, which is untrusted output: an absurd value crashed the run right
  after the write, so the executed fix never reached the record. The boundary is now the
  oracle's "before" probe time, and the controller no longer reads the tool's time at all.
  A namespace-wide action (possible only in the simulation-only safety-off ablation) marks
  every service as changed.
- **Blame toward a fixed dependency keeps counting.** A new epoch starts only because the
  oracle said "not recovered", so a fix on a dependency does not prove it is fixed. Callers'
  fresh blame stops counting only when fresh platform evidence shows the dependency healthy,
  and "healthy" now also needs p95 latency within `LATENCY_SLO_MS` (500 ms): a dependency
  that times out is not healthy even with few errors.
- **Stale means "insufficient", not "contradicted".** After a fix with no newer log lines,
  a claim that needs them is INSUFFICIENT. An event list cannot be dated, so after a fix no
  event list may prove "there were no OOM kills".
- **A write the target refused** changed nothing: it is judged with one probe, without the
  stabilization window.
- **A sent write is never lost.** The gateway traces `mitigation.started` before sending
  (write-ahead) and waits for the write's outcome even when the run is cancelled meanwhile.
  If the run is cancelled, or stopped while it waits for a live target to settle (its time
  ran out: BUDGET_EXHAUSTED), the controller still records the fix with its used approval,
  as `error`, "recovery not verified".
- **A provider's own `TimeoutError`** (for example from its HTTP client) is an error, not
  the run's deadline: only the run's clock produces BUDGET_EXHAUSTED. With no time left,
  no agent is started.

A residual gap: if the RECAP process is killed (not cancelled) between sending a write and
recording it, only the write-ahead trace event shows that the write was sent.

## Alternatives considered

- **A fixed sleep after every write.** Wastes time on fast targets and is still wrong for
  slow ones.
- **Drop all evidence after a fix.** Throws away history (deployments, onset times) that the
  next diagnosis needs.
- **Pause the run's clock while waiting.** Would let the total run time grow without bound
  across retries; the wait is bounded by the window and by the run budget instead.
