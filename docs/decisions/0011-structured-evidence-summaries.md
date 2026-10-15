# ADR 0011: Providers read structured evidence summaries, and no cut keeps only the head

**In one sentence:** the text a provider reads for a tool result is a deterministic summary
built from its structured data (newest errors, counts, metric extremes, times relative to the
alert), and wherever text must still be shortened, the beginning and the end are both kept.

## Status

Accepted (M1, 2026-10-15). Fixes audit finding 2.1.

## Context

Tool results are chronological: oldest first, newest last. V1 cut them twice, both times
keeping only the beginning: the gateway capped the raw JSON at 6,000 characters, then the
packer compacted every item to about its first 1,600. On the real simulator, 0 of 18 ERROR
lines of the inventory logs and 0 of 3 OOM errors of the payment logs reached the pack. The
scripted policy reads the full structured data, so every test passed; an LLM would have
read only INFO lines. A crowded proposal pack could also drop the runbook for the
diagnosis, turning a runbook-backed rollback into a blind restart.

## Decision

- [recap/context/summaries.py](../../recap/context/summaries.py): `render_for_agents(evidence,
  incident_time, max_chars)`.
  - Logs: line and level counts, the first ERROR and how long before the alert, the newest
    ERROR/WARN lines with repeats collapsed (`×k`), the most frequent message shapes.
  - Metrics: first, min, max and latest values with their times, the largest jump, and the
    share of the limit.
  - Health, deployments (with times relative to the alert), events grouped by service,
    type and reason, services, rollback plans and action outcomes: one short line each.
  - A summary is fitted into the item's room by showing fewer entries, so the newest are
    never the part that is cut. Unknown tools and data of an unexpected shape are shown raw.
  - It reports observations, never a diagnosis, and knows only RECAP's observation models,
    not the simulator's templates.
- The packer shows summaries; `Evidence.content` stays the raw record for the registry, the
  dashboard and audits.
- Compaction (packer) and the output caps (gateway) keep two thirds of the room for the
  beginning and one third for the end, with a pointer or mark in the middle.
- `ContextPacker.build(..., pinned_ids=...)`: pinned items are packed before any ranking.
  The controller pins the runbook chunks whose `Mechanism:` hint matches the diagnosis.

## Consequences

### Good

- All seven AUDIT-2.1 reproductions and the runbook-packing reproduction pass, end to end.
- Packs are much smaller. In the synthetic evaluation, mean estimated context tokens per
  case fell by 23 to 54% depending on the configuration (`full_adaptive`: 13,375 to 8,629),
  which matters for free-tier token-per-minute limits in M2. (Right after M1 it was 25 to
  57%; the review's "newest ERROR" line, below, costs a few percent.)

### Bad

- A summary can hide a detail that only the raw lines show (an INFO line between errors, an
  exact value in the middle of a series). The raw record is one ID away, but no tool lets a
  provider fetch it yet.
- Estimated token counts in evaluations dropped because packs are smaller; older reports
  are not comparable on that column.
- Summaries are rule-based extraction, not model-written summarization.

## Amended after the M1 review

- **The newest ERROR is always named.** A log summary lists the newest ERROR/WARN lines, so
  many newer warnings could crowd the newest error out of a tight summary. It now gets its
  own line ("newest ERROR: ...").
- **Untrusted numbers cannot break a summary.** NaN and infinity in tool data crashed the
  number formatting (`int(nan)`), which failed the whole investigation round. Metric
  statistics now use finite points only and say how many were left out; any value a
  summarizer still trips over falls back to the raw record instead of stopping the run.
- **The untrusted-data prefix has its own room.** A summary is fitted to the item's room
  minus the prefix the packer adds to flagged text, so the prefix can never push a fitted
  summary into compaction.

## Alternatives considered

- **Keep the newest N lines only.** Loses when errors began, which deploy-timing reasoning
  needs.
- **An LLM-written summary.** Costs a call per item, is not deterministic, and could be
  manipulated by the text it summarizes.
- **A bigger item cap.** Postpones the problem and spends the token budget.
