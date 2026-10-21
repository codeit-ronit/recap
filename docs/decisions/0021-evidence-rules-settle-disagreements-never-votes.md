# ADR 0021: Evidence rules settle the agents' disagreements, never votes; an open conflict gets one follow-up round, then a person

**In one sentence:** the verifier merges the agents' claims, records every disagreement
between them with the evidence and rule that settled it, chooses only by evidence rules
(a contradiction, grounding, the deepest root cause), and when two different supported root
causes are level it asks the next round one question built from checked values and then
hands the incident to a person instead of guessing.

## Status

Accepted (M3, 2026-10-21). Extends [ADR 0012](0012-platform-signals-outrank-log-text.md) (V6
now also covers the target) and the verifier rules of V1.

## Context

With independent specialists (ADR 0020) the merge step decides what RECAP believes. A
review of it before M3 found that it decided by accident or by volume:

- claims were merged on origin, mechanism, target and dependency, but not on the release
  version. Two claims naming different bad releases of one service became one claim that
  kept the version of whichever came first, so swapping the order of two findings turned a
  supported diagnosis into none;
- two supported actionable root causes at the same depth were ranked by how much evidence
  each cited. Padding a claim with unrelated citations could win the diagnosis;
- a claim was supported even if it cited none of the evidence its rule used (support from
  evidence it never looked at), and nothing recorded which agent's evidence decided;
- a resource-pressure claim on a service that was healthy through the whole incident was
  supported by one old OOM kill. It lost only because the other claim cited more;
- a rollback could name any version the environment's history accepts, not the one the
  evidence names as the restore point.

## Decision

1. **Merge** ([recap/agents/merger.py](../../recap/agents/merger.py), `merge_claims`). Same claim =
   same origin, mechanism, target and detail: the release version (the newest observed
   release when the claim names none, as V2 reads it) or the blamed dependency. Agents
   making the same claim pool their evidence; `HypothesisCheck.proposers` lists them all.
   Findings arrive in a fixed order (round, then the router's agent order), so the merge
   never depends on timing.
2. **Basis and grounding** ([recap/agents/verifier.py](../../recap/agents/verifier.py)). Every
   passing rule records the evidence it relied on (`basis_evidence_ids`). New rule
   **V1-grounding**: a supported claim must cite at least one of those items, else it stays
   insufficient. Support from another agent's evidence still counts, which is how two
   views combine: infrastructure cites the release, telemetry's error rate completes V2.
3. **V6 for the target.** An actionable claim on a service the platform shows healthy
   (every health snapshot and error-rate reading normal) is contradicted: there is nothing
   on it to fix.
4. **V7-deepest-cause, without tie-breaks.** Of the supported actionable claims, the one the
   most services' upstream errors lead to wins; live before remembered for the same cause.
   How much a claim cites, and how many agents propose it, decide nothing. Different root
   causes at the same depth stay an **open conflict**: no best hypothesis, status
   insufficient, focus on the competing services.
5. **Disagreement records** (`VerificationReport.disagreements`): `detail_conflict` (two bad
   releases of one service; settled by V2), `cross_agent_contradiction` (one agent's claim
   rejected with evidence another agent recorded, naming the rule and the evidence),
   `competing_root_causes` (settled by V7, or open) and `missing_view` (an agent of this
   epoch ended with an error or no usable answer, and made no claim). Each says whether it
   is resolved, by which rule, and in one sentence how.
6. **One follow-up round, then a person.** An open conflict carries a question: "Supported
   root causes compete: A and B. Find evidence that tells them apart: ...". It is built
   only from mechanism names, service names that look like service names and versions
   that look like versions; anything else becomes "(unnamed)". The next round routes by
   the new rule **RT3-disagreement** (unless an earlier rule fires first), and the question
   reaches each agent's plan, its `agent.start` event and its prompt, outside the untrusted
   data. If the conflict is still open when the rounds are used up, the run ends FAILED:
   "supported root causes still conflict (...) and no evidence rule decides between them;
   a human should take over". No fix is proposed.
7. **VP4-restore-point.** A fix that restores a version must restore one the evidence names:
   the version before the diagnosed release (its `previous_version`, or the release
   deployed just before it in the history) or the target of a `propose_rollback` dry run,
   and never the release the diagnosis blames.
8. **Trace.** `verification.merge` shows how many claims each agent made, what they merged
   into, which claims agents shared, the disagreements and the open questions; the
   `verification.evidence` checks now carry proposers and basis.

## Consequences

### Good

- The same findings in any order give the same diagnosis, and padding cannot win one.
- Every multi-agent outcome is explained: whose claim lost, to whose evidence, by which rule.
- When the evidence cannot decide, RECAP says so and stops before acting.
- The scripted policy's 45 baseline runs end exactly as before (0 differences in state,
  diagnosis, rounds, routing rules, actions and every claim's status).
- Revert checks: undoing any one of the seven changes makes its guarding test fail
  (`tests/unit/test_evidence_merger.py`, `tests/unit/test_verifier.py`,
  `tests/e2e/test_disagreement_follow_up.py`).

### Bad

- A real two-fault incident found in one round is handed to a person, although fixing both
  in turn could work. (When the faults are found in different epochs, RECAP fixes them in
  turn, as before.)
- Grounding asks for one cited basis item, not all of them: a claim citing everything it saw
  counts as grounded. Citations are capped at 8, and padding still cannot win a tie.
- The question gives a model the structure of the conflict, not the other agent's reasoning.
  The scripted policy ignores questions (its focus services already cover the competing
  services).
- Rule-ID lists of supported claims gained `V1-grounding`; tests expecting the old lists
  were updated.

## Alternatives considered

- **Majority vote, or weighting by stated confidence.** Two specialists agreeing on a
  contradicted claim would win, and a model's confidence is not evidence (RECAP never
  fabricates one).
- **An LLM judge between the agents.** Another untrusted, costly component deciding what the
  trusted rules could not; it could be talked into either answer.
- **Hand each agent the other's claim text.** It would carry model text (and anything a log
  line planted in it) into the trusted part of a prompt. The question carries the conflict's
  structure from checked values instead.
- **Require citing every basis item.** Each specialist sees only half of the evidence, so no
  single claim could ever cite it all.
