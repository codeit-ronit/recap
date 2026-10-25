# ADR 0023: Memory keeps each kind of run apart, and reuse is decided by live evidence alone

**In one sentence:** scripted runs, real-model runs and evaluation runs each have their own
memory partitions; an episode now summarizes its run's trajectory by deterministic
extraction; and a remembered fix is "safe to reuse" only when today's evidence confirms the
same mechanism on the same service and the fix itself still fits, however similar the
alert looks.

## Status

Accepted (M3, 2026-10-21). Owner decision: separate logical partitions for scripted demos,
real-model verified episodes and isolated evaluation runs. Extends
[ADR 0010](0010-label-free-views-and-a-yes-no-oracle.md) (episodes stay label-free for
providers). The M3 review (2026-10-25) corrected and extended decisions 1 to 5.

## Context

- Every run wrote to the `demo` partition, so a real model's episodes mixed with the
  scripted policy's; a replay of a recorded run became new "experience".
- "Safe to reuse" meant: a verified success with the same mechanism. A verified rollback of
  checkout was "safe" for a payment incident, a cart restart "safe" for inventory, and a
  rollback to `v2.4.0` "safe" when today's evidence names a different restore point (or
  when `v2.4.0` is today's bad release).
- An episode stored the diagnosis and the actions, not what the run checked, which claims
  it rejected, or the timing behind its diagnosis.
- Measured on 2026-10-21 over the 15 scenarios' alerts: alerts with the same cause and with
  different causes are equally alike (`nomic-embed-text` cosine, median 0.777 against
  0.761; word overlap, median 0.273 against 0.333), and several alerts with different causes
  are word for word the same (by design: `memory-caution` reads like `bad-deployment`). No
  similarity can tell causes apart.

## Decision

1. **Partitions by origin** (`MemoryAccess.for_origin`, resolved by the controller when a
   run does not choose): the scripted policy reads TRAIN and DEMO and writes DEMO; a real
   model reads TRAIN and the new LIVE partition and writes LIVE; a replay never writes (its
   experience was the original run's) and reads memory as of the moment its recording
   began (`MemoryAccess.as_of`, from the recording header's `created_at`; the `memory.search`
   event shows it as `created_before`), so it never finds the episode its own original run
   wrote. The evaluation keeps choosing explicitly: training runs use TRAIN, test runs read
   TRAIN only and never write, and held-out scenarios never become episodes. Opening a
   memory file from before M3 moves each DEMO episode that consolidator-v1 wrote for a real
   model (provenance provider `<provider>:<model>`, replays and red-team wraps of a model
   included) to LIVE, in the column and in the stored episode. Each move is recorded in
   `partition_moves`. Scripted episodes and every M3 episode stay where they are.
2. **Consolidator v2** ([recap/memory/consolidation.py](../../recap/memory/consolidation.py)):
   an episode also holds its trajectory (one step per piece of evidence: agent, call,
   evidence ID), every claim the verifier judged (status, proposers, rules, failed lines,
   and the version or dependency that told it apart), the passing rules' lines of the
   supported diagnosis itself (the supported check with the diagnosis' detail; before the M3
   review a rejected release's claim on the same service could supply them), the fix's
   preconditions (action, target, mechanism, bad and restored version), and the model's
   origin, name, prompt version and weights. `summary_method` says
   `deterministic-extraction`: RECAP's own records, no model writing about itself.
3. **Reuse v2** (`assess_reuse`, [recap/memory/store.py](../../recap/memory/store.py)): safe
   needs every condition: a success, verified by the oracle, the same mechanism, on the
   same service (both the past diagnosis and the fix that worked; an episode that names no
   service is not safe), and for a rollback a version that today's evidence names as a
   restore point (the verifier's `restore_points`, the same rule as VP4) and that is not
   today's bad release (a remembered rollback that names no plain release cannot be
   checked, so it is not safe either). Each hit records the conditions that held and those
   against reuse; hits are re-ranked by what live evidence confirmed (safe first), not by
   similarity.
4. **Semantic recall, not semantic trust:** with hybrid retrieval the incident's embedding is
   stored with its episode; a later run embedding its own incident with the same model also
   finds up to k (3) episodes whose cosine is at least 0.8 (a paraphrased alert), listed
   after the k lexical hits, closest first, so they never push a lexical hit out. Before
   the M3 review they were ranked with the lexical hits and cut whenever k episodes alerted
   on the same service filled the list on the service score alone. The
   decision-relevant `similarity` (routing's "familiar", the scripted policy's memory
   shortcut) stays lexical.
5. **Plain values in trusted text:** RECAP's reuse verdict and the proposal prompt's
   memory lines repeat only plain identifiers from an episode (an ID, a service, a version);
   anything else is shown as "(withheld)". A fix's parameter names are repeated only when
   the trusted catalog gives that action such an argument, and an episode ID is offered to
   the model and accepted as a citation only when it is shown (prompts-v4.6, after the M3
   review). An episode is read from a file on disk, and a tampered one must not be able to
   write trusted prompt text.

## Consequences

### Good

- A real model's experience is its own, and a scripted demo never reads it (also in a data
  folder from M2: its model episodes move from DEMO to LIVE when the store opens it).
- The scripted policy's memory-on baseline (train scenarios first, then all; adaptive and
  parallel; 30 runs, before and after) ends exactly as before in state, diagnosis, routing,
  actions and proposal source. What changed: 16 hits' reuse verdicts, and each change was
  checked by hand: a fix for another service, or a rollback to a version today's
  evidence does not name, is no longer "safe".
- The deliverable's "memory reuse or justified rejection" is visible per hit: which
  conditions held and which did not (`memory.assessment` events, the run record).

### Bad

- Fewer hits are "safe to reuse", so any metric counting them drops; that is the point.
- Semantic recall is untested on real paraphrased alerts (every pair of scenario alerts
  already shares words); the 0.8 recall bar is a guess, documented as one. A hybrid run can
  now carry up to 2k memory hits (6 at the defaults) into its prompts and context packs.
- Pre-M3 model episodes are recognized by their provenance provider name
  (`<provider>:<model>`); one edited by hand stays where it is.
- A replay reads memory as of its recording's start, but in another data folder than its
  original it can still see other memory. Only replaying with memory off is exact there.
- prompts-v4.5 (memory is shown with RECAP's verdict); the CI fixture was re-recorded. Its
  first five requests match v4.4's once the random incident and run IDs are normalized, but
  the model's diagnosis cited different evidence IDs, so the proposal prompt differed too.
  Its 351 output tokens (320 in v4.4) therefore say nothing about whether local inference
  repeats exactly. (Corrected after the M3 review; an earlier version said the prompts were
  byte-identical.)
- An LLM summary of a trajectory (allowed by the master prompt when free inference permits)
  is not built: deterministic extraction is cheaper, exact and cannot invent reasoning.

## Alternatives considered

- **One partition with an origin tag.** Every reader would have to filter; one forgotten
  filter mixes scripted and model experience.
- **Let semantic similarity mark an episode "familiar".** The measurement shows similarity
  does not separate causes; trusting it more would route harder incidents to one agent.
- **Keep reuse at "same mechanism".** It called a checkout rollback safe for payment.
