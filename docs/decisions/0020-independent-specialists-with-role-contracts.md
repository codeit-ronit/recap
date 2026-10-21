# ADR 0020: Parallel specialists work independently, under role contracts, with order-free evidence IDs

**In one sentence:** in a parallel round each specialist sees what was known before the round
plus its own results, never the other's in-flight evidence; it can conclude only the failure
mechanisms its own tools can observe; and evidence IDs and their order depend on the agent,
not on which agent finished first, so a recorded parallel run replays offline.

## Status

Accepted (M3, 2026-10-21). Extends [ADR 0016](0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md)
(prompts-v4.3) and [ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md) (parallel
replay).

## Context

Before M3 the two specialists of a parallel round shared everything as it happened:

- each agent's context pack held the other agent's evidence the moment it was recorded, so
  the second opinion was often the first one repeated: the views were not independent;
- evidence IDs were numbered in arrival order (`ev-3f9a2c-007`). The same run with
  different timing gave different IDs, different prompts and so different model requests,
  and a recording of a parallel run failed to replay (`ReplayMismatch`) whenever the live
  timing differed from the replay's;
- the roles overlapped (both read health) and either could claim any mechanism, including
  one it had no tool to observe (telemetry claiming a bad release it never saw);
- one slow specialist could use the whole run's clock, and when the run's budget stopped,
  the finding the other specialist had already completed was lost.

## Decision

1. **Round-scoped views** ([recap/harness/loop.py](../../recap/harness/loop.py),
   `AgentHarness.visible`). During a parallel round an agent sees the evidence recorded
   before the round plus its own; "already observed" lists only those calls. After the
   round everyone sees everything, and the verifier always reads all of it.
2. **Order-free evidence IDs** ([recap/context/evidence.py](../../recap/context/evidence.py)).
   `open_round` gives each agent its own counter (`ev-3f9a2c-r2t01` for telemetry's first
   result in round 2, `-r2i01` for infrastructure's); `close_round` puts the round's
   evidence in agent order, then each agent's own order. Single-agent rounds keep the old
   sequential numbers. The run store lists evidence in that order (`ORDER BY rowid`).
3. **Role contracts** ([configs/agents.yaml](../../configs/agents.yaml)). Telemetry reads
   logs and metrics; infrastructure reads releases, cluster events, health and topology:
   disjoint tool sets. Each role lists its responsibilities (rendered in its prompt) and the
   mechanisms it may conclude (telemetry: resource pressure, dependency failure, unknown;
   infrastructure adds bad deployment). The harness drops a claim outside the role's list
   for every provider, scripted or model, and the trace says so (`dropped_out_of_role`).
   The schema offers only the allowed mechanisms, so a model cannot even select another.
4. **Honest findings** ([recap/core/models.py](../../recap/core/models.py)). A hypothesis
   may name `contradicting_evidence_ids` and a `confidence_note` (the model's own words,
   untrusted, shown to people and never used to decide). `AgentFinding.outcome` says how
   the agent ended: hypothesis, need_more_evidence, stop, step_limit, no_usable_answer or
   error.
5. **Bounded and fault-tolerant rounds** ([recap/routing/orchestrator.py](../../recap/routing/orchestrator.py)).
   In a parallel round each agent may use at most `parallel_agent_time_share` (0.7) of the
   time left, so a slow specialist ends as an error finding and the other's survives. When
   the shared budget stops the run, findings other agents completed travel with the stop
   and are kept in the record, labelled partial (never consolidated into memory). Each
   provider guards its one-time readiness check with a lock, so two concurrent agents never
   check the model twice.

## Consequences

### Good

- Two specialists are now two views: each claim rests on what its own tools observed, and
  the verifier combines them (ADR 0021).
- A recorded parallel run replays offline whatever the live timing was
  (`tests/e2e/test_parallel_replay.py` delays either specialist in the live run).
- The scripted policy's 45 baseline runs (15 scenarios, adaptive/single/parallel) end
  exactly as before.

### Bad

- An independent specialist knows less while its round runs. In a two-fault incident the
  infrastructure agent cannot claim a bad release in round 1, because the error evidence
  it needs is in telemetry's view; RECAP then fixes one fault, sees "not recovered" and
  finds the other in the next epoch (observed with the scripted policy).
- prompts-v4.3 changes every request fingerprint, so the CI replay fixture recorded with
  v4.2 must be recorded again once the M3 prompts are final.

## Alternatives considered

- **Keep the shared live view and only fix the IDs.** Replay would still break: the prompts
  would still depend on what the other agent had recorded so far.
- **A fixed schedule (one agent waits for the other).** Deterministic, but it is no longer
  parallel investigation, and the second agent is not independent either.
- **Trust the prompt alone to keep roles apart.** A model, or a compromised planner, can
  ignore an instruction; the harness enforces the contract for every provider.
