# `recap/agents`: who investigates, and who checks their claims

| File | What it does |
|---|---|
| `roles.py` | The investigation roles and their specs from `configs/agents.yaml` (`agent_specs`). |
| `verifier.py` | `EvidenceVerifier`: trusted, rule-based checks of root-cause claims and proposed fixes. It implements the `Verifier` protocol in `recap/core/interfaces.py`, which is all the controller depends on. |
| `merger.py` | How the verifier combines several agents' claims: merges the same claim, picks the deepest supported root cause (V7) without any vote, records every disagreement, and writes the next round's questions and leads from checked values only. |

Agents do not have their own classes. Every role runs the same observe → reason → act loop
(`recap/harness/loop.py`); a role only changes the goal, the read-only tool list, the
mechanisms it may conclude and the step limit. What an agent *decides* comes from the
provider: by default the scripted baseline policy, which is **not an LLM**.

## Roles

| Role | Tools (all read-only) | May conclude | Runs when |
|---|---|---|---|
| `investigator` | every read tool, and `search_runbooks` | any mechanism | single-agent mode |
| `telemetry_investigator` | logs, metrics, and `search_runbooks` | resource pressure, dependency failure, unknown | parallel mode, beside the next one |
| `infrastructure_investigator` | services, health, deployments, events, rollback dry run, and `search_runbooks` | bad deployment too | parallel mode |
| `evidence_verifier` | none: it is plain code, not a looping agent | (judges the others) | after every investigation round |
| `controller` | the only writer, always through the safety gateway | | proposing and executing a fix |

The two specialists' environment tools are disjoint (ADR 0020), so each can conclude only what
it can observe; the harness drops a claim outside a role's list for every provider.
`search_runbooks` is answered by RECAP's own runbook index, not by the environment, so both
may use it. Loading the config refuses to start if a worker role lists a write tool.

## The evidence verifier

Providers propose hypotheses generously (recall). The verifier decides which one the evidence
really supports (precision). It is trusted code: it shares only the observation models and the
signal conventions in `recap/core/signals.py` with the scripted policy; its rules are written
independently, so a mistake in the policy's heuristics is not repeated in the check. It reads
**all** tool evidence of the run from the evidence registry, not only what a claim cites. That
is how one agent's evidence can contradict another agent's claim, and how two specialists'
views combine (infrastructure cites the release, telemetry's error rate completes V2).

> This is structural / evidence-rule validation, **not proof of causation**. A supported
> claim agrees with simple rules about timing and platform signals; it is not proven.

**Observed healthy** (used by V2, V3, V4, V6 and V7): at least one health snapshot or
`error_rate` series exists for the service; every snapshot shows `Running` with all replicas
ready, an error rate at or below 5% and a p95 latency at or below the 500 ms SLO; and no
`error_rate` point goes above 5% and no latency point above the SLO. Log lines are text that
anyone can write (audit finding 2.2: three forged `OutOfMemoryError` lines got a healthy service
restarted in V1), so they never outweigh what the platform itself reports. Blame toward an
observed-healthy service also gives it no "deepest root cause" bonus.

| Rule | A claim passes when... | Example line shown in the dashboard |
|---|---|---|
| `V1-structural` | it names a target and a known mechanism, cites evidence, and every cited ID exists in **this** run | `the claim cites evidence that is not in this run: ev-made-up-1` |
| `V1-grounding` | a supported claim cites at least one evidence item its rules relied on (its basis). Any listing of the same release, log line or OOM kill counts, whichever was recorded first | `the claim cites none of the evidence its rules relied on (ev-1-001, ev-1-003)` |
| `V2-deploy-timing` | bad_deployment(S, v): S deployed v, and S's errors began 5 min before to 30 min after it | `errors on checkout began 90 min after v2.5.0 was deployed (allowed window -5..+30 min)` |
| `V2-upstream-dominance` | ...and fewer than half of S's ERROR lines blame a dependency that is **not observed healthy** | `7 of 7 ERROR lines of checkout say 'upstream ... call failed': its errors come from payment` |
| `V3-resource` | resource_pressure(S): OOM kills or memory ≥ 90% of the limit. OOM log lines only corroborate: alone they leave the claim insufficient (contradicted when memory stays < 70% and the events show no OOM kill, or when S is observed healthy) | `payment's memory reached 98% of its 512 MB limit (pressure from 90%)` |
| `V4-dependency` | dependency_failure(S → D): S's logs show its calls to D failing (`upstream D call failed`, `rpc to D failed` and the other phrasings in `signals.py`) and D is not observed healthy. Never a fix target by itself: it points the search at D | `not actionable by itself, and nothing on payment is supported yet` |
| `V5-memory` | a remembered claim faces the same rules: no free pass | `live evidence does not confirm what ep-057647f525 remembered` |
| `V6-conflict-resolution` | when log **text** disagrees with platform signals (health snapshots, metrics, cluster events), the platform signals win, and the check names the evidence on both sides. It also covers the target: an actionable claim on an observed-healthy service is contradicted, as there is nothing on it to fix | `24 ERROR lines of checkout blame payment, but payment is observed healthy (ev-1-007, ev-1-008); platform signals outrank log text, so they are not counted` |
| `V7-deepest-cause` | of the supported actionable claims, the one the most services' upstream errors lead to wins (`merger.py`). How much a claim cites, or how many agents propose it, decides nothing | `bad deployment on payment (v1.13.0) is the deepest root cause: the upstream errors of 2 other services lead to payment, more than to checkout` |

Status of one hypothesis: **one contradiction rejects it** (CONTRADICTED); otherwise any missing
piece leaves it INSUFFICIENT; only a claim with everything in place is SUPPORTED. An error onset
only counts if the evidence also shows a calm moment before it; when a metric is already
failing at its first point, the verifier says "the onset is unseen" instead of guessing.

### Merging the agents' claims (`merger.py`)

Same claim = same origin (live or remembered), mechanism, target and detail: the release
version for a bad deployment (the newest observed release when the claim names none; a
version written `2.4.1` or `V2.4.1` for the observed `v2.4.1` is that release), the blamed
dependency for a dependency failure. Agents making the same claim pool their evidence and are
all listed as its proposers. Two different versions of one release are judged apart.

### The report

* `best`: the SUPPORTED `bad_deployment` or `resource_pressure` claim that is the deepest root
  cause (V7: the service the most other services' upstream errors lead to), live investigation
  before memory for the same cause. Different root causes at the same depth are **not**
  ranked by how much evidence they cite (padding could win that): they stay an open
  `competing_root_causes` conflict, `best` is empty and `status` insufficient. The next round
  gets one question about the conflict (RT3-disagreement); if it is still open after that, the
  run ends FAILED for a person, without proposing a fix.
* `status`: `supported` if there is a best claim, `contradicted` if every claim is contradicted,
  otherwise `insufficient`.
* `checks`: one per distinct claim, with `passed` / `failed` lines, `rule_ids`, `proposers`,
  the `detail` that told it apart (see above) and, for a supported claim, `basis_evidence_ids`
  (the evidence its passing rules relied on, every listing of it). A remembered claim is kept
  apart so the dashboard shows it judged on its own.
* `disagreements`: every conflict between the agents and how it ended. `detail_conflict` (two
  versions of one release: settled by `V2-deploy-timing` when one is supported, otherwise by
  the rules that contradicted the others), `cross_agent_contradiction` (one agent's claim
  rejected with evidence another agent recorded), `competing_root_causes` (settled by V7, or
  open) and `missing_view` (an agent ended with an error or no usable answer). An open one
  carries a question for the next round (`report.open_questions()`). If a question is still
  open when the rounds are used up and nothing is diagnosed, the controller ends the run
  FAILED, worded by what is open: "supported root causes still conflict" for level
  supported causes, "claims still disagree on which release is bad" for an open
  `detail_conflict`.
* `leads`: where the next round should look when nothing conflicts. A supported dependency
  failure (S's calls to D fail) that no supported cause on D or below it explains, with nothing
  on D itself supported either, gives one lead per D: "checkout's calls to payment fail and
  nothing on payment itself is confirmed yet: find out what is wrong with payment (its errors,
  memory, restarts and releases)." A lead is not a disagreement and changes no claim. The
  controller passes the leads to the next round's agents after the open questions, and traces
  them (`leads` in `investigation.round`, `verification.evidence` and `verification.merge`),
  but only open questions set the signal that routes RT3-disagreement. Leads were added after
  the first real `qwen3:8b` parallel runs (prompts-v4.5) confirmed such a failing call in
  round 1 and did not examine the dependency itself in round 2 (not yet re-measured with
  prompts-v4.6).
* `focus_services`: where the next round should look (the services of an open conflict,
  services with missing evidence, the dependency an unresolved dependency failure points at,
  or the dependency that a victim's errors come from). The controller passes them to the
  agents of the next round; a model's prompt repeats only the names its tools allowlist,
  since the verifier may have read a name from a log line.

Questions and leads are read by a model as RECAP's own (trusted) text, so they are built only
from mechanism names, service names shaped like a DNS label and versions in the tool
registry's `vX.Y.Z` shape; anything else is written `(unnamed)`, and a lead whose names fail
the check is not made at all.

### Example: the memory-caution demo

Memory remembers "bad deployment on checkout" from an earlier run. Live evidence disagrees:
checkout's release is 95 minutes old and all its errors say `upstream payment call failed`.
Round 1 contradicts the remembered claim and asks to look at payment; round 2 finds OOM kills
on payment, so the best claim becomes resource pressure on payment and the fix is a restart,
not the remembered rollback.

### Checking a proposed fix (`check_proposal`)

`VP1-evidence-exists` (cites evidence, all from this run), `VP2-target-matches-diagnosis`,
`VP3-action-named`, and `VP4-restore-point`: a fix that restores a version must restore one
the evidence names (the release before the diagnosed one, or the target of a
`propose_rollback` dry run), never the release the diagnosis blames. Whether the action is
*safe* is not the verifier's job: the safety policy decides that, before this check runs.

### Limits (honest)

The rules read a fixed family of log phrasings (`recap/core/signals.py`): the simulator's
templates and a few common client-library styles. A log style outside that family hides which
service blames which; a real cluster adapter would need parsers for its own log formats, or an
LLM provider that reads raw text. The verifier's rules and report would stay the same.
