# ADR 0029: One evaluation record per run, modes that never share a number, and a named reason for every run that did not recover

**In one sentence:** every evaluated run becomes one `EvalRecord` built only from the run's
own record and trace; records are grouped in batches of exactly one mode (scripted
regression, offline replay or real model; a real cluster is M5 and has no records); a run is
a success only when the independent oracle verified the recovery, and every other run carries
one failure category chosen by fixed, generic rules.

## Status

Accepted (M4, 2026-10-26). Builds on [ADR 0010](0010-label-free-views-and-a-yes-no-oracle.md)
(the scenario's answer never reaches a model), [ADR 0017](0017-fail-closed-free-tier-guard-and-a-deferred-state.md)
(the free-tier guard), [ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md)
(recordings) and [ADR 0028](0028-content-addressed-recordings-and-replay-by-manifest-id.md)
(recordings pinned by their bytes).

## Context

- At the end of M3, RECAP had three kinds of evaluation output, each in its own shape: the
  scripted V1 benchmark (`evals/reports/latest/results.json`), the live plans' summaries
  (`scripts/live_eval.py`, `summary.json`) and single exported runs (`evals/live/`). They
  could not be listed side by side, and nothing kept a reader, or a dashboard, from adding
  the scripted policy's successes to a real model's.
- A failed run said only FAILED. Why (the model never looked at the cause, it named the
  wrong mechanism, a quota stopped it, a person said no) had to be read out of the trace by
  hand, as `docs/status.md` did for each M3 run.
- M4 asks for evaluations that keep scripted, replayed, real-model and (later) real-cluster
  results apart; that track each run's model, prompts, requests, reported tokens, time,
  tool calls, agent topology, retrieval, memory, claims, fixes, gateway outcome, oracle
  verdict, failure reason and configuration; that categorize failures; that control cost;
  and that test safety against a fooled model, including that safety does not refuse a
  correct repair.

## Decision

1. **One record per run, one mode per batch** ([evals/records.py](../../evals/records.py)).
   `EvalRecord` holds one run; `EvalBatch` holds the runs of one plan, all of one mode:
   `scripted_regression`, `offline_replay` or `real_llm`. `make_batch` refuses a record of
   another mode. `kubernetes_future` exists only so a report can say what is missing:
   `make_batch` and `record_from_run` refuse it.
2. **Only what the run recorded** ([evals/categorize.py](../../evals/categorize.py),
   `record_from_run`). The record is built from the RunRecord and the trace, nothing else:
   - tokens are the counts the model server reported (`None` when it reported none), and
     RECAP's own estimate is a separate field; a replayed answer adds no request, no token
     and no model time;
   - stage times come from the state transitions, and the wait for an approver
     (`AWAITING_APPROVAL`) is its own stage, apart from RECAP's own work;
   - `model_seconds` sums the request durations the trace recorded;
   - the mode must fit where the decisions came from (the scripted policy, a replay, a local
     or free hosted model) and the target must be simulated, or the record is refused;
   - a recording is named by its file name, never its path; free text (an error, a note) is
     cut down to file names and cleaned of secret-shaped values; and `publish_batch` refuses
     a batch that still names a local folder or holds a secret-shaped value.
3. **Success needs the oracle.** `success` is true only for RESOLVED with the oracle's
   verified recovery. A RESOLVED state without that verdict is categorized `error`.
4. **A failure category for every other end** (`categorize`), first match wins. From the
   end state: `cancelled`, `quota_cap`, `budget_exhausted` (a run limit),
   `approval_rejected` (a "no" or an expired request), `safety_blocked` (the policy, the
   evidence gate or the namespace allowlist). Then, for a FAILED run: `error` for a replay
   that left its recording (ReplayMismatch: the recording, not the model, no longer
   matches), `oracle_not_recovered` (a fix ran and the system stayed unhealthy),
   `budget_exhausted` (every agent of the last round ran out of its time share),
   `provider_failure` (a provider error), `error` (any other error), `open_disagreement`
   (supported causes still in conflict), `provider_failure` (no usable answer, or no usable
   fix for a supported diagnosis). An investigation that ended without a supported cause is
   compared with the scenario's
   predeclared root service R and mechanism M, which no model ever sees (ADR 0010):
   `missed_crucial_tool` (no tool call named R), `wrong_mechanism` (claims named R, none
   with M), `wrong_service` (no claim names R, not even as the failing dependency of a link
   the verifier did not contradict), else `insufficient_evidence`. The rules read lifecycle
   states, rule verdicts, tool arguments and enum values, never model text, and name no
   scenario or service; a test renames the services and gets the same categories.
5. **Who writes batches.**
   - `scripted_regression`: [evals/runner.py](../../evals/runner.py) writes `batch.json` next
     to `results.json` (the scored test and red-team runs; the train runs only build memory);
     `update_latest` also publishes it under `evals/reports/batches/<report id>/`;
     `python -m evals.runner --rebuild-batch DIR` rebuilds the batch of an earlier report from
     the runs its own data folder kept.
   - `offline_replay`: [scripts/eval_replay.py](../../scripts/eval_replay.py) replays every
     recording in `fixtures/recordings/MANIFEST.json` (each checked against its pinned
     SHA-256) in a fresh data folder of its own, with the routing, memory and retrieval its
     entry names.
   - `real_llm`: [scripts/live_eval.py](../../scripts/live_eval.py) writes one batch per plan
     run, after every run; `--from-data` builds the batch of finished real-model runs already
     in a data folder, without calling a model, and refuses a folder whose runs used more than
     one model or prompt version.
   Local batches go to `<data folder>/evals/batches/<batch_id>/`; published ones (in git) to
   `evals/reports/batches/<batch_id>/`, with a `README.md` that is the batch's own digest.
   Published batches accumulate: nothing deletes one.
6. **Cost controls before anything runs** (`scripts/live_eval.py`): nothing live starts while
   the `CI` variable is set; a hosted free plan needs `--confirm-free-plan` for each batch, on
   top of the guard's own checks (opt-in variable, key, dated confirmation); the provider is
   built once before the first run, which checks those without sending anything; a quota
   stop or the free plan's tripwire ends the batch (exit 3): the stopped run is recorded and
   the later runs are named as not started.
7. **Server time.** `OllamaProvider` fills `ModelCall.server_ms` from Ollama's
   `total_duration` (nanoseconds). A request's duration minus the server's own time is time
   spent elsewhere: mostly queued, when two agents ask a server that answers one request at
   a time. The record keeps only the summed request time; the split is in the trace.
8. **Safety and robustness are evaluated, not assumed.**
   [tests/adversarial/test_m4_safety.py](../../tests/adversarial/test_m4_safety.py) fools a
   fake model with each lure of the adversarial incident (an instruction in the logs, a
   poisoned runbook, a planted memory) at each place it could act, with every approval
   answered yes, and checks each guardrail on its own: the evidence rules, the proposal
   schema, the evidence gate, the policy and the environment's capability check. Its
   false-positive test gives the same lures to a model that reads the evidence correctly: the
   fix reaches approval, runs, and the oracle verifies it.
   [tests/integration/test_m4_robustness.py](../../tests/integration/test_m4_robustness.py)
   breaks one thing per test (a recording cut short, malformed and unknown evidence IDs,
   trace events out of order, no token counts, a rejected and an expired approval, a
   cancelled run, runbook vectors of another embedder, a destructive write) and checks that
   the run ends cleanly and the record names it.

## Consequences

### Good

- Scripted, replayed and real-model results can be shown together without sharing a number:
  each batch has one mode, its sample size and its limitations.
- Every published number is a count of records that can be checked against the runs they
  came from. The scripted batch rebuilt from the committed V1 report agrees with its
  `results.json` on all 32 scored runs (state, verified recovery, tool calls).
- Failures have names. Applied to the M3 real-model runs (built from their stored records),
  the rules give the same reading as the hand analysis in `docs/status.md`: the two
  cross-service failures are `wrong_mechanism`, the unfamiliar runs that blamed frontend or
  cart are `wrong_service` or `missed_crucial_tool`, and the Groq adversarial run is
  `quota_cap`.
- Each guardrail has a test of its own, and so does the promise that they let a correct fix
  through.

### Bad

- The investigation categories need a predeclared answer. An incident without one (a real
  cluster's, until someone writes it down before the run) only ever gets
  `insufficient_evidence` for an unsolved investigation.
- One category per run: a second problem in the same run shows only in the detail text.
- A rebuilt batch judges old runs with today's rules; its methods say when and with which
  code it was rebuilt, and the categories can change when the rules do.
- `EvalRecord` has no field yet for a run's label (an evaluation configuration's name, a
  plan's run id) or for its server time. Until it does, the scripted batch lists its
  configurations together, in order, and its methods say how to tell them apart.
- The samples are tiny (one run per configuration in every real-model batch), and the
  target is simulated. A batch shows counts, never rates.

## Alternatives considered

- **A model as the judge of failures.** An evaluation would then depend on untrusted
  inference, and could not be repeated exactly; fixed rules over recorded facts can.
- **One table for every mode, with a "source" column.** It invites one headline number
  across modes, which is exactly what must not happen.
- **Re-running the M3 plans to get batches.** Hours of inference, and different samples: the
  recorded runs are the evidence, and their records hold everything a batch needs.
- **Categories computed in the dashboard.** Each reader would need the same rules; in Python
  next to the record they are tested once, and every reader gets the same answer.

## Update (M5, 2026-11-03)

The placeholder `kubernetes_future` became `real_kubernetes`: a real model's runs on the real
Kind cluster, each fix approved by a person, built from the stored runs with
`scripts/live_eval.py --from-data`. The rule of this decision holds: `record_from_run` puts a
run against a real cluster in that mode only and refuses it in every other mode, and
`make_batch` still keeps one mode per batch.
