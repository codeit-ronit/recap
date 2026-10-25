# Episodic experience memory (`recap/memory/`)

RECAP has two kinds of "context from the past", and they are kept apart on purpose:

| | Runbook RAG (`recap/knowledge/`) | Episodic memory (this package) |
|---|---|---|
| Holds | generic operational docs written by people | what RECAP itself saw and did in earlier runs |
| Unit | a Markdown section (`DocChunk`) | one finished run (`ExperienceEpisode`) |
| Search | BM25 over text (or hybrid with a local embedder) | deterministic word overlap + service match; with an embedder, semantic recall can add close paraphrases |
| Trust | untrusted text with citations | untrusted historical data, never instructions |

In V1 every episode comes from the **simulated** environment, and its provenance says so.

## Where it sits in a run

```
incident --MemoryQuery--> ExperienceStore.search(access) --> hits: "similar, not yet checked"
                                                                   |
live evidence --> EvidenceVerifier --> VerificationReport --> assess_reuse(hit, report, restore_points)
                                                                   |
     safe_to_reuse = past SUCCESS and oracle-verified and same mechanism and same service
                     and the fix still fits today's evidence (see below)

run ends --> RecoveryOracle --> RunRecord --> Consolidator.build_episode --> ExperienceStore.add(access)
```

`memory_conflict(hits, threshold)` tells the router when similar episodes disagree
(different mechanism or different outcome), which is a reason to investigate more broadly.
`rank_hits` orders checked hits by what live evidence confirmed: safe to reuse first, then a
matching mechanism, then similarity.

## Partitions: each kind of run has its own memory

`MemoryAccess` says which partitions a run reads and where it writes. When a run does not
choose (`RunConfig.memory_read_partitions` / `memory_write_partition`), the controller uses
`MemoryAccess.for_origin` with the origin of the run's decisions:

| Run | Reads | Writes |
|---|---|---|
| scripted policy (`scripted`) | TRAIN + DEMO | DEMO |
| a real model (`local_llm`, `free_hosted_llm`, `paid_llm`) | TRAIN + LIVE | LIVE |
| a replay of a recorded model run (`replay`) | TRAIN + LIVE | nothing: it is not new experience |
| evaluation training (`eval_train`) | TRAIN | TRAIN |
| evaluation test (`eval_test`) | TRAIN | nothing |

So a real model's experience is its own: a scripted demo never reads it, and a model run
never reads the scripted policy's. `access.as_of(moment)` reads memory as it was at a
moment (episodes created at or after it are left out). The controller uses it for a
replay, with the recording header's `created_at`, so a replay reads what its original run
could have read, and not the episode that run wrote when it ended; the `memory.search`
event shows the bound as `created_before`.

**Files from before M3.** Until M3 every run wrote DEMO, so an older `memory.db` holds model
episodes where scripted runs read them. When `ExperienceStore` opens a file, it moves each
DEMO episode that consolidator-v1 (before M3) wrote for a real model, its provenance
`provider` being `<provider>:<model>` (for example `ollama:qwen3:8b`, or
`replay:ollama:qwen3:8b` for a recorded model's answers), to LIVE, in the partition column
and in the stored episode; nothing else about it changes. Each move is recorded in the
`partition_moves` table (episode, original and new partition, reason, time):
`sqlite3 data/memory.db 'SELECT * FROM partition_moves'`. The scripted policy's episodes
(`deterministic-scripted-policy`), and every episode since M3 (whose partition its run
chose), never move. Opening the file again moves nothing more.

## Similarity (`similarity.py`)

```
similarity = 0.7 * jaccard(symptom words) + 0.3 * service score      (rounded to 3 decimals)
service score = 1.0 same alerted service | 0.5 episode service is affected now | 0 otherwise
```

Words are lowercase letters/digits, at least 3 characters, minus stopwords and pure numbers:
`"checkout: HTTP 5xx ratio above 30% (SLO 1%)"` becomes `{checkout, http, 5xx, ratio, slo}`.
The score is a pure function of its inputs, so the same store and query always give the same
hits in the same order (similarity, then newest, then episode ID).

`search(query, access, k=3, min_similarity=0.2)` returns up to `k` episodes at least
`min_similarity` alike. This `similarity` is the one that decides anything (routing's
"familiar", the scripted policy's memory shortcut).

**Semantic recall.** When the run embeds its incident (hybrid retrieval), each episode stores
the embedding of its own incident and the model that made it. After the `k` lexical hits,
up to `k` more follow: episodes embedded by the same model whose cosine with today's
incident is at least `SEMANTIC_RECALL` (0.8, a guess: every pair of scenario alerts already
shares words), closest first, however few words they share. A recalled hit keeps its lexical
`similarity` (often below `min_similarity`), so it can never make an incident look
familiar, and it never pushes a lexical hit out. Each hit's `signals` shows both scores.
Measured on the scenarios, alerts with different causes are as alike as alerts with the
same cause, so no similarity, lexical or semantic, is evidence of the cause.

## Similar is not safe (`assess_reuse`)

A search hit starts with `safe_to_reuse=False` and the reason
`"similar symptoms; not yet checked against live evidence"`. After the verifier has judged
this run's evidence, `assess_reuse(hit, report, restore_points)` marks a hit safe only if
**every** condition holds:

1. the past run's outcome was `success`,
2. the independent recovery oracle verified that recovery,
3. the past diagnosis has the same mechanism that live evidence supports now,
4. on the same service: the past diagnosis and the fix that worked were both on the service
   diagnosed now (an episode that names no service does not pass),
5. the remembered fix still fits today's evidence: a rollback restores one of today's
   `restore_points` (the versions the verifier finds in this run's evidence, the same rule
   as VP4), and not the release diagnosed as bad now. A remembered rollback that names no
   plain release cannot be checked, so it does not pass.

A high similarity never outweighs a failed condition. Each checked hit lists the
conditions that held (`supporting`) and those against reuse (`contradicting`), and its
reason says them in plain words, for example:

- `safe to reuse: live evidence also points to bad deployment and the past fix was independently verified`
- `similar symptoms and the past fix was independently verified, but live evidence points to resource pressure, not bad deployment`
- `similar symptoms and live evidence also points to bad deployment and the past fix was independently verified, but the live diagnosis is on payment, not on checkout`
- `..., but the remembered rollback restores v2.4.0, the release diagnosed as bad now`

The verdict is shown to models as RECAP's own (trusted) text, so it repeats only plain
identifiers from an episode (an ID, a service, a version); anything else is `(withheld)`.
A fix's parameter names are repeated only when the trusted catalog gives that action such
an argument, and an episode ID is offered to the model and accepted as a citation only when
it is shown. Only the *kind* of action is reused (for example `execute_rollback`); its
target and parameters still come from live evidence, and it still goes through the safety
gateway.

## What an episode holds (`consolidation.py`, consolidator-v2)

Besides the incident, the verified diagnosis, the actions and the oracle's verdict, an
episode summarizes its run by deterministic extraction from RECAP's own records
(`summary_method = "deterministic-extraction"`; no model writes about itself):

- `trajectory`: one step per piece of evidence (agent, call, evidence ID);
- `checked_claims`: every claim the verifier judged, with its status, proposers, rules and
  `detail` (the release version or blamed dependency that told it apart from other claims
  of its mechanism on its service);
- `diagnosis_basis`: the passing rules' lines of the supported diagnosis itself (the check
  that is supported and has the diagnosis' detail, never a rejected release's claim);
- `fix_preconditions`: what must hold for the fix to apply again (action, target,
  mechanism, bad and restored version);
- `provenance`: simulated or not, environment, provider, model origin, name, prompt version
  and weights, retrieval mode, seed, config and fixture hashes, origin run and trace link.

## Contamination controls

| Risk | Control |
|---|---|
| Scoring on what you trained on | `MemoryAccess.eval_test()` reads TRAIN only and cannot write |
| Test runs creating memory | `add` refuses any TEST-partition episode |
| A held-out test scenario finding its own past runs | `Consolidator.build_episode` never builds an episode from a test scenario, whatever partition the run writes to, so even a demo or API run of one leaves nothing for later runs to reuse (the controller traces this as `memory.skip`) |
| Demo runs leaking into evaluation | `eval_train()` and `eval_test()` never read DEMO or LIVE episodes |
| A real model's experience mixing with the scripted policy's | model runs read and write LIVE, scripted runs DEMO; neither reads the other's (episodes written before M3 are moved, see above) |
| A replay counted as new experience | a replay never writes memory |
| Agent text claiming success | `outcome=success` needs `verified=True` from the oracle; `add` rejects anything else |
| A crash or a cancellation remembered as experience | the controller does not consolidate a run that ended by an error or a cancellation (traced as `memory.skip`) |
| Ground-truth labels reaching agents | `failure_family` is stored for isolation and analysis, but never used in hit reasons |

Every episode also records `scenario_id`, `failure_family`, `partition` and `origin_run_id`,
plus its provenance (above) and a link to the run's trace (`runs/<run_id>/events`).

## Using it

```python
from recap.memory.consolidation import Consolidator
from recap.memory.similarity import MemoryQuery
from recap.memory.store import (
    ExperienceStore,
    MemoryAccess,
    assess_reuse,
    memory_conflict,
    rank_hits,
)

store = ExperienceStore(settings.paths.memory_db)
# A run reads and writes its own kind of memory: the scripted policy DEMO, a real model LIVE.
access = MemoryAccess.for_origin(provider.info.origin)

hits = store.search(MemoryQuery.from_incident(incident), access)
conflict = memory_conflict(hits, settings.budget.routing.conflict_similarity)
# ... investigate, then verify this run's evidence ...
target = report.best.target_service if report.best else None
points = verifier.restore_points(target, report, registry) if target else None
hits = rank_hits([assess_reuse(hit, report, points) for hit in hits])

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

- The deciding similarity is lexical: "OOM" and "out of memory" do not match. Semantic
  recall can add such an episode, but only when the run embeds its incident, and only as a
  candidate to check. A hybrid run can therefore carry up to `2k` hits (6 at the defaults)
  into its prompts.
- A replay in another data folder than its original run can still see other memory (the
  `as_of` bound hides only what came later). Replaying with memory off is the exact choice
  there.
- No forgetting, decay or de-duplication: each finished run adds one episode.
- The reuse check compares the mechanism, the service and the fix, not the full causal
  story. It is evidence-rule validation, not proof.
- Evidence summaries are copied from untrusted tool output, so they stay untrusted when
  packed into an agent's context.
- One local SQLite file, opened per operation, and `search` scores every readable episode in
  Python. That is simple and thread-safe for hundreds of episodes, not built for millions.
