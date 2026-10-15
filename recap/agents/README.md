# `recap/agents`: who investigates, and who checks their claims

| File | What it does |
|---|---|
| `roles.py` | The investigation roles and their specs from `configs/agents.yaml` (`agent_specs`). |
| `verifier.py` | `EvidenceVerifier`: trusted, rule-based checks of root-cause claims and proposed fixes. It implements the `Verifier` protocol in `recap/core/interfaces.py`, which is all the controller depends on. |

Agents do not have their own classes. Every role runs the same observe → reason → act loop
(`recap/harness/loop.py`); a role only changes the goal, the read-only tool list and the step
limit. What an agent *decides* comes from the provider: by default the scripted baseline
policy, which is **not an LLM**.

## Roles

| Role | Tools (all read-only) | Runs when |
|---|---|---|
| `investigator` | every read tool | single-agent mode |
| `telemetry_investigator` | health, logs, metrics | parallel mode, beside the next one |
| `infrastructure_investigator` | services, deployments, events, rollback dry run, health | parallel mode |
| `evidence_verifier` | none: it is plain code, not a looping agent | after every investigation round |
| `controller` | the only writer, always through the safety gateway | proposing and executing a fix |

Loading the config refuses to start if a worker role lists a write tool.

## The evidence verifier

Providers propose hypotheses generously (recall). The verifier decides which one the evidence
really supports (precision). It is trusted code: it shares only the observation models and the
signal conventions in `recap/core/signals.py` with the scripted policy; its rules are written
independently, so a mistake in the policy's heuristics is not repeated in the check. It reads
**all** tool evidence of the run from the evidence registry, not only what a claim cites. That
is how one agent's evidence can contradict another agent's claim.

> This is structural / evidence-rule validation, **not proof of causation**. A supported
> claim agrees with simple rules about timing and platform signals; it is not proven.

**Observed healthy** (used by V2, V3, V4 and the ranking): at least one health snapshot or
`error_rate` series exists for the service, every snapshot shows `Running` with all replicas
ready and an error rate at or below 5%, and no `error_rate` point goes above 5%. Log lines are
text that anyone can write (audit finding 2.2: three forged `OutOfMemoryError` lines got a
healthy service restarted in V1), so they never outweigh what the platform itself reports.
Blame toward an observed-healthy service also gives it no "deepest root cause" bonus.

| Rule | A claim passes when... | Example line shown in the dashboard |
|---|---|---|
| `V1-structural` | it names a target and a known mechanism, cites evidence, and every cited ID exists in **this** run | `the claim cites evidence that is not in this run: ev-made-up-1` |
| `V2-deploy-timing` | bad_deployment(S, v): S deployed v, and S's errors began 5 min before to 30 min after it | `errors on checkout began 90 min after v2.5.0 was deployed (allowed window -5..+30 min)` |
| `V2-upstream-dominance` | ...and fewer than half of S's ERROR lines blame a dependency that is **not observed healthy** | `7 of 7 ERROR lines of checkout say 'upstream ... call failed': its errors come from payment` |
| `V3-resource` | resource_pressure(S): OOM kills or memory ≥ 90% of the limit. OOM log lines only corroborate: alone they leave the claim insufficient (contradicted when memory stays < 70% and the events show no OOM kill, or when S is observed healthy) | `payment's memory reached 98% of its 512 MB limit (pressure from 90%)` |
| `V4-dependency` | dependency_failure(S → D): S's logs show `upstream D call failed` and D is not observed healthy. Never a fix target by itself: it points the search at D | `not actionable by itself, and nothing on payment is supported yet` |
| `V5-memory` | a remembered claim faces the same rules: no free pass | `live evidence does not confirm what ep-057647f525 remembered` |
| `V6-conflict-resolution` | when log **text** disagrees with platform signals (health snapshots, metrics, cluster events), the platform signals win, and the check names the evidence on both sides | `24 ERROR lines of checkout blame payment, but payment is observed healthy (ev-1-007, ev-1-008); platform signals outrank log text, so they are not counted` |

Status of one hypothesis: **one contradiction rejects it** (CONTRADICTED); otherwise any missing
piece leaves it INSUFFICIENT; only a claim with everything in place is SUPPORTED. An error onset
only counts if the evidence also shows a calm moment before it; when a metric is already
failing at its first point, the verifier says "the onset is unseen" instead of guessing.

### The report

* `best`: the SUPPORTED `bad_deployment` or `resource_pressure` claim, ranked by the deepest root
  cause (the service other services' upstream errors point at), then more evidence, then live
  investigation over memory.
* `status`: `supported` if there is a best claim, `contradicted` if every claim is contradicted,
  otherwise `insufficient`.
* `focus_services`: where the next round should look (services with missing evidence, the
  dependency an unresolved dependency failure points at, or the dependency that a victim's
  errors come from). The controller passes them to the agents of the next round.
* `checks`: one per distinct claim, with `passed` / `failed` lines and `rule_ids`. Agents that
  propose the same claim are merged (their evidence IDs are pooled); a remembered claim is
  kept apart so the dashboard shows it judged on its own.

### Example: the memory-caution demo

Memory remembers "bad deployment on checkout" from an earlier run. Live evidence disagrees:
checkout's release is 95 minutes old and all its errors say `upstream payment call failed`.
Round 1 contradicts the remembered claim and asks to look at payment; round 2 finds OOM kills
on payment, so the best claim becomes resource pressure on payment and the fix is a restart,
not the remembered rollback.

### Checking a proposed fix (`check_proposal`)

`VP1-evidence-exists` (cites evidence, all from this run), `VP2-target-matches-diagnosis`,
`VP3-action-named`. Whether the action is *safe* is not the verifier's job: the safety policy
decides that, before this check runs.

### Limits (honest)

The rules read the simulator's signal formats (`recap/core/signals.py`). A real cluster adapter
would need parsers for its own log formats, or an LLM provider that reads raw text; the
verifier's rules and report would stay the same.
