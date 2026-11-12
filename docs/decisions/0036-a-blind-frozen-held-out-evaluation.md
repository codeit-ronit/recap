# ADR 0036: A blind, frozen held-out evaluation: no answer before the runs, a pinned model, and a freeze checked before every run

**In one sentence:** RECAP's held-out SREGym evaluation is chosen from the benchmark's problem
names alone by a rule written down first, its scenario files carry no answer until the runs are
over, its model is pinned to one weights file, and one hash over everything that decides RECAP's
behaviour is checked before every scored run.

## Status

Accepted (M6, W5 preparation; 2026-11-09). Builds on [ADR 0031](0031-an-external-benchmark-harness-release-tags-and-pod-failure-onsets.md)
(SREGym's external harness owns the fault), [ADR 0023](0023-memory-partitions-by-origin-and-evidence-checked-reuse.md)
(held-out test scenarios are never consolidated) and [ADR 0035](0035-one-switch-comparisons-separate-component-datasets-nothing-pooled.md).
The protocol is [w5-protocol.md](../w5-protocol.md); the manifest is
[evals/frozen/w5-heldout-sregym.yaml](../../evals/frozen/w5-heldout-sregym.yaml).

## Context

M5 ran RECAP on one SREGym problem, fixed the adapter on it, and wrote that problem's answer
into its scenario file from SREGym's source. That is right for a development problem and wrong
for a held-out one: writing the answer means reading it, and a scenario schema that requires an
answer forces that. A real-model evaluation also depends on things a commit does not pin: the
model tag on a server (a re-pull changes its weights), the installed packages, and the files a
person might edit between runs.

## Decision

1. **Selection by name, by a rule written first.** Only the benchmark registry's problem names
   are read. The rule (the app RECAP's profile allows, never used before, a fault in what RECAP
   may write, a symptom RECAP can measure) and every candidate's decision are committed before
   any run; none is replaced after a result.
2. **Blind scenario files.** A held-out external problem may have `family: unlabelled` and
   `expected: {}`, and only such a problem (test partition, external harness). Its answer is
   written after the evaluation, from the benchmark's own definition, and the batch is judged
   again with it. Its id and title are neutral; none of its fields reaches an agent.
3. **A pinned model.** An Ollama provider may name its weights digest (`weights:`); the provider
   refuses a server whose model is another file. The VM's provider is pinned to the weights M5
   used.
4. **A freeze, checked.** `scripts/w5_freeze.py` hashes every tracked file under the frozen roots
   (code, configs, prompts, runbooks, scenarios, the SREGym integration, the approval path, the
   run and batch scripts) and the installed packages; the manifest records both. The check runs
   before every scored run, and a test fails while the evaluation is frozen if a frozen file
   differs. A change before the first scored run is a new freeze in a new commit; none after it.
5. **Memory cannot carry an answer between problems.** Held-out runs are never consolidated, the
   scored data folder starts empty, and the setup pilot has a folder of its own.
6. **Cost is gated by time, not by billing data.** A meter estimates spend from the VM's own state
   and pinned prices, conservatively, and no new step starts past a stop line below the cap.

## Consequences

### Good

- Nobody, including the evaluator, read a held-out answer before the runs; the files prove it.
- A re-pulled model, an edited runbook or an upgraded package is caught before a scored run.
- Every attempted problem is reported, including those that never deploy or never alert.

### Bad

- Five problems chosen by name may include ones RECAP cannot fix with a rollback or a restart;
  that is part of what is measured, and the report says which. Observed (2026-11-09): all three
  problems the evaluation reached were of that kind, and one sat outside the profile's allowlist
  ([w5-protocol.md](../w5-protocol.md#results)).
- Categories that need the answer (wrong service, wrong mechanism) exist only after the post-hoc
  labelling; until then a failed run reads as unsolved.
- The freeze also blocks harmless edits under the frozen roots for the evaluation's duration.

## Alternatives considered

- **Read the problems' definitions to pick ones RECAP can fix.** Rejected: that is selecting on
  the answer, which is exactly what a held-out evaluation must not do.
- **Write the answers before the runs but hide them from agents.** Rejected: the agents never see
  them anyway; the point is that the evaluator did not see them either.
- **Trust the commit hash alone.** Rejected: the model's weights and the packages are not in it.
