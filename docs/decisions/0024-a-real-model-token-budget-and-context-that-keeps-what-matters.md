# ADR 0024: A real model-token budget, and context that keeps what matters under pressure

**In one sentence:** a run's model tokens are budgeted by what the model server actually
reports, reserved before each request so parallel agents cannot overshoot together, and
when space runs out, a long log keeps its errors and the evidence a diagnosis needs is
shortened rather than dropped.

## Status

Accepted (M3, 2026-10-21). Extends [ADR 0011](0011-structured-evidence-summaries.md)
(structured summaries; no cut keeps only the head) and the run budget of the V1 harness.

## Context

- The run budget limited steps, tool calls, wall-clock time and the ESTIMATED size of the
  context packs. Real models report the tokens they read and wrote; those were counted in
  `Usage` but limited by nothing, so a long real-model run could spend without bound.
- The gateway cut a long log list to its newest 60 lines. When a service kept logging INFO
  noise after its errors, the cut removed every error before RECAP stored the evidence: the
  audit's "truncation that discards all errors", one step before the packer.
- The packer ranked the evidence a diagnosis rests on, and evidence that contradicts a
  claim, above everything else. But when such items together exceeded the pack budget,
  the last one was simply skipped.

## Decision

1. **`RunBudget.max_model_tokens`** (200,000 by default, `configs/budget.yaml`):
   `BudgetTracker.reserve_model_tokens` before every model request (its estimated prompt
   plus `max_output_tokens`), `settle_model_tokens` after it with the server's own input and
   output counts (the prompt estimate for an answer that reported none, nothing for a 429 or
   a 5xx). Reservations of requests in flight count, so parallel agents cannot overshoot
   together. Running out raises BudgetExhausted before the request is sent; the run ends
   BUDGET_EXHAUSTED, and the run summary shows `model_tokens` next to the limit. The
   controller binds the budget to the provider (`bind_token_budget`); a replay settles with
   the recorded counts, so it stops where the original run did.
2. **Errors first when a log list is cut** (`_errors_first` in
   [recap/tools/gateway.py](../../recap/tools/gateway.py)): the first ERROR line (the
   onset), then the newest ERROR and WARN lines up to half of the room, then the newest
   lines of any level; the kept lines stay in time order. Other lists are cut as before.
3. **Must-keep items are shortened, not dropped** ([recap/context/packer.py](../../recap/context/packer.py)):
   a pinned or priority item that no longer fits is compacted into the room left (its
   beginning and end, with the pointer to the full record) when at least 32 tokens remain.
   Lower-ranked items never get this.

## Consequences

### Good

- A real-model run has a hard ceiling on the tokens its servers report, enforced before
  spending, with the real counts.
- An incident's error lines survive a noisy log, and contradicting evidence survives a
  crowded pack.
- No prompt template changed: the CI replay still matches, and both scripted baselines (45
  runs without memory, 30 with) end exactly as before.
- Revert checks: each of 5 changes (the reservation, settling with the server's counts, the
  errors-first cut, the must-keep shortening, counting reservations in flight) is guarded
  by a test that fails when it is undone.

### Bad

- The reservation uses RECAP's rough estimate (about 4 characters per token) plus the full
  answer allowance, so a run can stop a little before the real total would reach the limit.
- The optional Claude adapter is not an `HttpLLMProvider`, so this budget does not bind to
  it (it is disabled and needs separate paid opt-in anyway).
- The tool itself still returns only the newest lines it was asked for (`limit`); an agent
  must ask for enough. A level filter on the log tool would change the environment's API.

## Alternatives considered

- **Count real tokens after the fact only.** A run could overshoot by a whole decision,
  and two parallel agents by two.
- **Keep only ERROR lines when cutting.** The newest INFO lines show whether a service has
  recovered; the cut keeps both.
- **Shrink every item when the pack is full.** Many small fragments of low-ranked evidence
  are worth less than the evidence a diagnosis rests on, kept whole.
