# Episodic experience memory (`recap/memory/`)

RECAP has two kinds of "context from the past", and they are kept apart on purpose:

| | Runbook RAG (`recap/knowledge/`) | Episodic memory (this package) |
|---|---|---|
| Holds | generic operational docs written by people | what RECAP itself saw and did in earlier runs |
| Unit | a Markdown section (`DocChunk`) | one finished run (`ExperienceEpisode`) |
| Search | BM25 over text | deterministic word overlap + service match |
| Trust | untrusted text with citations | untrusted historical data, never instructions |

In V1 every episode comes from the **simulated** environment, and its provenance says so.

## Where it sits in a run

```
incident --MemoryQuery--> ExperienceStore.search(access) --> hits: "similar, not yet checked"
                                                                   |
live evidence --> EvidenceVerifier --> VerificationReport --> assess_reuse(hit, report)
                                                                   |
                    safe_to_reuse = past SUCCESS and oracle-verified and same mechanism as now

run ends --> RecoveryOracle --> RunRecord --> Consolidator.build_episode --> ExperienceStore.add(access)
```

`memory_conflict(hits, threshold)` tells the router when similar episodes disagree
(different mechanism or different outcome), which is a reason to investigate more broadly.

## Similarity (`similarity.py`)

```
similarity = 0.7 * jaccard(symptom words) + 0.3 * service score      (rounded to 3 decimals)
service score = 1.0 same alerted service | 0.5 episode service is affected now | 0 otherwise
```

Words are lowercase letters/digits, at least 3 characters, minus stopwords and pure numbers:
`"checkout: HTTP 5xx ratio above 30% (SLO 1%)"` becomes `{checkout, http, 5xx, ratio, slo}`.
The score is a pure function of its inputs, so the same store and query always give the same
hits in the same order (similarity, then newest, then episode ID).

## Similar is not safe (`assess_reuse`)

A search hit starts with `safe_to_reuse=False` and the reason
`"similar symptoms; not yet checked against live evidence"`. After the verifier has judged
this run's evidence, `assess_reuse` marks a hit safe only if **all three** hold:

1. the past run's outcome was `success`,
2. the independent recovery oracle verified that recovery,
3. the past diagnosis has the same mechanism that live evidence supports now.

The reason says which conditions passed or failed, in plain words, for example:

- `safe to reuse: live evidence also points to bad deployment and the past fix was independently verified`
- `similar symptoms and the past fix was independently verified, but live evidence points to resource pressure, not bad deployment`

Only the *kind* of action is reused (for example `execute_rollback`); its target and
parameters still come from live evidence, and it still goes through the safety gateway.

## Contamination controls

| Risk | Control |
|---|---|
| Scoring on what you trained on | `MemoryAccess.eval_test()` reads TRAIN only and cannot write |
| Test runs creating memory | `add` refuses any TEST-partition episode |
| A held-out test scenario finding its own past runs | `Consolidator.build_episode` never builds an episode from a test scenario, whatever partition the run writes to, so even a demo or API run of one leaves nothing for later runs to reuse (the controller traces this as `memory.skip`) |
| Demo runs leaking into evaluation | `eval_train()` and `eval_test()` never read DEMO episodes |
| Agent text claiming success | `outcome=success` needs `verified=True` from the oracle; `add` rejects anything else |
| A crash or a cancellation remembered as experience | the controller does not consolidate a run that ended by an error or a cancellation (traced as `memory.skip`) |
| Ground-truth labels reaching agents | `failure_family` is stored for isolation and analysis, but never used in hit reasons |

Every episode also records `scenario_id`, `failure_family`, `partition` and `origin_run_id`,
plus provenance: `simulated`, `environment`, `provider`, `seed`, `config_hash`,
`fixture_hash`, `created_by` and a link to the run's trace (`runs/<run_id>/events`).

## Using it

```python
from recap.memory.consolidation import Consolidator
from recap.memory.similarity import MemoryQuery
from recap.memory.store import ExperienceStore, MemoryAccess, assess_reuse, memory_conflict

store = ExperienceStore(settings.paths.memory_db)
access = MemoryAccess.from_run_config(run_config)

hits = store.search(MemoryQuery.from_incident(incident), access)
conflict = memory_conflict(hits, settings.budget.routing.conflict_similarity)
# ... investigate, then verify this run's evidence ...
hits = [assess_reuse(hit, report) for hit in hits]

# Raises ValueError for an unfinished run or a held-out test scenario.
episode = Consolidator().build_episode(
    record=record,
    evidence=registry.all(),
    scenario=scenario,
    partition=access.write_partition,
    simulated=record.environment.is_simulated,
    environment_kind=record.environment.kind,
)
stored = store.add(episode, access)  # False when this run may not write memory
```

The memory code never imports an environment. It only sees `RunRecord`, `Evidence` and the
environment's `kind` label, so swapping the simulator for a SREGym/Kubernetes adapter does not
change this package.

## Limitations (V1) and planned upgrades

- Similarity is lexical: "OOM" and "out of memory" do not match. An embedding score could
  replace `episode_similarity` without changing the store or its rules.
- No forgetting, decay or de-duplication: each finished run adds one episode.
- The reuse check compares the mechanism, not the full causal story. It is evidence-rule
  validation, not proof.
- Evidence summaries are copied from untrusted tool output, so they stay untrusted when
  packed into an agent's context.
- One local SQLite file, opened per operation, and `search` scores every readable episode in
  Python. That is simple and thread-safe for hundreds of episodes, not built for millions.
