# ADR 0026: A recording keeps where timing stopped the run, and a replay stops exactly there

**In one sentence:** which of two parallel agents reaches a shared limit first, when a clock
cuts an agent short, and whether a request gets an answer at all are timing, which a replay
cannot reproduce, so a recorded run writes each such stop (and each request that got no
answer) into its recording at a place that does not depend on timing, and a replay, which
never refuses on its own and has no clock, stops exactly there.

## Status

Accepted (M3 review, 2026-10-25). Corrects a claim of
[ADR 0020](0020-independent-specialists-with-role-contracts.md) (a recorded parallel run
replays whatever the live timing was) and one of
[ADR 0024](0024-a-real-model-token-budget-and-context-that-keeps-what-matters.md) (a replay
stops where the original run did). Extends
[ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md).

## Context

ADR 0020 made a parallel round's prompts and evidence IDs independent of timing, so a
recorded parallel run replays offline. The M3 review found that this held only while timing
stopped nothing. A replay answers every request at once, so:

- a specialist that its time share (0.7 of the time left) cut short in the live run is never
  cut in the replay: it goes on, asks for something the recording does not hold, and the
  replay ends FAILED with `ReplayMismatch`;
- which agent a shared budget (tool calls, model tokens) refuses first depends on how the
  agents interleave. In a replay they interleave differently and their model requests no
  longer overlap, so the replay refuses another agent, or nobody, and goes off its
  recording again;
- a request that got no answer (the server could not be reached, or did not answer in time)
  was not recorded. The live run went on (a failed tool call, a retry); the replay had no
  answer for it.

The same review found two gaps in what a stop leaves behind: a model request cancelled while
it was out kept its token reservation for the rest of the run, and when the budget refused a
corrective retry, the request already answered went missing from the trace and the usage.

## Decision

1. **Every stop gets a place that does not depend on timing**
   ([recap/harness/budget.py](../../recap/harness/budget.py)): the actor (`ACTOR`: the agent
   whose turn `AgentHarness.run_agent` is running, or `controller` outside any turn) and how
   many checks that actor had made: turn starts (`begin_turn`), budget checks, model and
   embedding requests (`check_request`) and named waits (`checkpoint`). An agent makes the
   same checks in the same order in a replay, whatever the other agent does, because its
   prompts do not depend on the other's timing (ADR 0020).
2. **A recorded run writes its stops into its recording**, as `stop` lines between the
   exchanges ([recap/providers/transport.py](../../recap/providers/transport.py),
   `bind_stops`): a *refusal* (any `BudgetExhausted` the tracker raises, and the
   orchestrator finding no time left to start an agent) and a *cut* (a clock cancelled what
   the actor's last check allowed: an agent's time share or the run's deadline in the
   orchestrator, or the run's deadline around a provider call in the controller,
   `_before_deadline`). The wait for a live target to settle has a checkpoint of its own, so
   a refusal there has a place too.
3. **A replay never refuses on its own and has no clock** (`replay_stops`): no limit check
   refuses, `remaining_seconds` is unbounded and no agent gets a timeout. It raises exactly
   the recorded stops at the same places: a refusal as `BudgetExhausted`, a cut as
   `ReplayedCut` ([recap/core/errors.py](../../recap/core/errors.py)). `ReplayedCut` is a
   `BaseException`, like the cancellation it stands for, so no handler of ordinary errors
   catches it; the code whose clock cut the original turns it back into what that cut
   became: the orchestrator into the same timeout finding or `BudgetExhausted`, the
   controller into `BudgetExhausted`. The replay's counters still count, so its budget totals
   end as the original's did. A replay that makes a different check where the recording put
   a stop ends with `ReplayMismatch` ("record the run again").
4. **A request that got no answer is recorded** (`failed`, with the error's text), and the
   replay raises the same `ProviderError` for it, so what the run did next lines up.
5. **What a stop leaves on the record**
   ([recap/providers/http_llm.py](../../recap/providers/http_llm.py)): a model request
   cancelled while it was out settles its reservation with at least its prompt's estimate
   (the server may have read the prompt), instead of keeping it reserved; when the budget
   refuses a corrective retry, the request already answered goes on the record (an
   `llm.call` event and usage) with the stop.
6. **`ReplayMismatch` says what to check:** something the run reads changed since it was
   recorded (the prompts, tools, tool results or memory), or the recording predates
   recorded stops and timing stopped its run. Memory is the other input a replay must read
   as the original did: since the same review a replay reads it as of its recording's start
   ([ADR 0023](0023-memory-partitions-by-origin-and-evidence-checked-reuse.md)).

## Consequences

### Good

- A parallel run that a time share or a shared budget stopped replays to the same end: the
  same agent gets the same timeout finding, or the run stops with the same budget message,
  with the same findings, actions, tool calls and budget counts
  (`tests/e2e/test_parallel_replay.py`: a time-share cut, and stops on tool calls and on
  model tokens).
- A request that failed live fails the same way in the replay, which then goes on as the
  live run did (`tests/e2e/test_replayed_failures.py`).
- A run without a recording (the scripted policy) refuses exactly as before and writes
  nothing (`tests/unit/test_budget_stops.py`). No prompt changed for this.
- A reservation no longer outlives its request, and no answered request is missing from the
  record (`tests/unit/test_http_llm_budget.py`, `tests/e2e/test_model_token_budget.py`).

### Bad

- A replay reads its stops from the recording. A recording made before this change has none,
  so it replays as before, except that it never refuses on its own: an old recording of a
  run that a budget or a clock stopped now ends with `ReplayMismatch` and must be recorded
  again. (The committed CI fixture's run was stopped by nothing.)
- A place counts checks. A change that adds or removes a budget check moves the places, so a
  recording with stops must then be recorded again; the replay says so instead of guessing.
- Places rely on ADR 0020: an agent's checks must not depend on the other agent's timing.
  Anything shared that changes an agent's path by timing must be recorded too. The runbook
  searches of a run are such a thing: the controller's search budget (6 per run) asks
  `BudgetTracker.allow`, so a recorded run writes each refused search at its place and a
  replay refuses exactly there. A new shared allowance must do the same.

## Alternatives considered

- **Replay with the recorded timing (wait as long as each original answer took).** Slow, and
  still not exact: the event loop need not schedule two agents the same way twice, and a
  share of a different "time left" moves the cut.
- **Serve the agents' requests in the recorded order.** The replay would then depend on one
  scheduling of the event loop, and a stop would still need a place in the recording.
- **Record a stop by its time (seconds into the run).** A replay's answers come at once, so
  "41 s into the run" names no point in it.
- **Replay only runs that timing did not stop.** The runs most worth checking (a budget stop,
  a timeout) would be the ones nobody can check.
