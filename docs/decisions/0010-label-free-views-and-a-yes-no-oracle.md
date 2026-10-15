# ADR 0010: Providers get label-free views, and the oracle only ever says yes or no

**In one sentence:** a provider (the scripted policy today, an LLM later) receives
`AgentIncident` and `AgentMemoryHit` views that have no field for a scenario ID or an
evaluation label, and after a failed fix the next round's focus comes from RECAP's own
reasoning, never from the recovery oracle's list of unhealthy services.

## Status

Accepted (M1, 2026-10-15). Fixes audit finding 2.3.

## Context

Two leaks reached providers in V1:

- After a failed fix the controller set the next round's focus to
  `after.unhealthy_services`, the oracle's private ground truth. The prompt renderer showed
  it as "RECAP's verifier asked to look closer at: …". In `bad-deployment` it named
  `frontend`, a service no agent had looked at.
- `InvestigationRequest` and `ProposalRequest` carried the full `Incident` (with
  `scenario_id`) and full `MemoryHit`s (with each episode's `scenario_id` and
  `failure_family`). The prompts happened to skip those fields, but any provider that read
  or dumped the request object had the answer key. A scenario's name often *is* its answer.

An LLM provider is coming in M2, so "the renderer does not print it" is not a guarantee.

## Decision

- New contracts in [recap/core/models.py](../../recap/core/models.py): `AgentIncident`
  (alert facts only), `AgentEpisode` (the past run's observable story: symptoms, its
  verified diagnosis, what was tried, the oracle's verdict) and `AgentMemoryHit` (RECAP's
  reuse verdict plus an `AgentEpisode`). Each record has an `agent_view()` method.
- The provider-facing requests use field types `ProviderIncident` / `ProviderMemoryHits`
  whose validators reduce a full record to its view, so even a caller that passes the full
  `Incident` produces a label-free request. Views forbid extra fields.
- The packer takes the views too, so it cannot put a label into a pack.
- `_retry_focus` in [recap/harness/controller.py](../../recap/harness/controller.py): after a
  fix that did not recover, focus = the failed fix's service target + the verifier's focus
  services. The oracle's verdict is used only as a yes/no.
- The prompt section is now "Services to examine first (named by RECAP's verifier or by an
  earlier fix attempt)" (`prompts-v3`).

Trusted code (routing signals, the run record, the trace, memory's own store) keeps the full
records: they are not provider-facing.

## Consequences

### Good

- The two AUDIT-2.3 reproductions pass: a sentinel name that only the oracle reports never
  appears in a request or prompt, and no request holds a label field.
- The guarantee is in the types, not in the renderer's good behaviour.

### Bad

- A retry no longer gets a free hint about which other services are broken. It has to find
  them by observing, which can take more tool calls. That is the honest cost.
- Two parallel model families (full and view) must be kept in sync when a field is added.

## Alternatives considered

- **Strip labels in the prompt renderer only.** That was V1's state: one forgotten field or
  one new provider that serializes the request leaks the answer.
- **Delete labels from the records.** Evaluation and memory isolation need them.
