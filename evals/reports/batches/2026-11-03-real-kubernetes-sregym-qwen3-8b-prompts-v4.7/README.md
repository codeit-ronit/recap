# Real model on a real Kubernetes cluster (sregym): qwen3:8b, prompts-v4.7

> **Real model on a real Kubernetes cluster (Kind). REAL target: a Kubernetes cluster, not production.** N = 5: counts, not rates; no statistical claim.

- Batch `2026-11-03-real-kubernetes-sregym-qwen3-8b-prompts-v4.7` (mode `real_kubernetes`), created 2026-11-03T19:52:03.613710+00:00
- Plan: none (see methods)
- Providers: `ollama-vm:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| approval_rejected | 1 of 5 |
| safety_blocked | 1 of 5 |
| success | 1 of 5 |
| wrong_mechanism | 2 of 5 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds | server seconds (waited inside) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-e08c17d46e` | `sregym-misconfig-app-hotel-res` | single (investigator) | bm25 | on | BLOCKED | safety_blocked | not verified | 8 | 31,837 / 869 | 8,152 | 8 | 1188.4 (0.0) | 1183.8 | 1183.0 (0.6) |
| `run-6ffe22367d` | `sregym-misconfig-app-hotel-res` | single (investigator) | bm25 | on | BLOCKED | approval_rejected | not verified | 8 | 32,351 / 922 | 8,314 | 8 | 16543.1 (15345.2) | 1193.0 | 1192.3 (0.6) |
| `run-69e580846e` | `sregym-misconfig-app-hotel-res` | single (investigator); parallel (telemetry + infrastructure) | bm25 | on | FAILED | wrong_mechanism | not verified | 9 | 35,294 / 984 | 8,608 | 7 | 1450.0 (0.0) | 2225.8 | 2224.8 (204.2) |
| `run-cb90973e56` | `sregym-misconfig-app-hotel-res` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | off | FAILED | wrong_mechanism | not verified | 10 | 41,056 / 1,463 | 10,211 | 10 | 1525.3 (0.0) | 2944.8 | 2943.5 (312.4) |
| `run-9d7f47104d` | `sregym-misconfig-app-hotel-res` | single (investigator) | bm25 | off | RESOLVED | success | verified | 5 | 18,437 / 736 | 4,960 | 5 | 930.2 (121.9) | 728.4 | 727.7 (1.4) |

## Why each run ended as it did

- `run-e08c17d46e` (sregym-misconfig-app-hotel-res): **safety_blocked**: proposal is not supported by recorded evidence. Diagnosis `bad_deployment@geo`; fix: execute_rollback on geo: APPROVAL_REQUIRED (R11-reversible-approval); evidence check refused: the proposal restores 20260912.1, the release the diagnosis blames; not executed.
- `run-6ffe22367d` (sregym-misconfig-app-hotel-res): **approval_rejected**: the approval request expired without a decision. Diagnosis `bad_deployment@geo`; fix: execute_rollback on geo: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; approval expired, not executed.
- `run-69e580846e` (sregym-misconfig-app-hotel-res): **wrong_mechanism**: the claims on geo named resource_pressure, not bad_deployment. Diagnosis none; fix: none proposed.
- `run-cb90973e56` (sregym-misconfig-app-hotel-res): **wrong_mechanism**: the claims on geo named dependency_failure, resource_pressure, not bad_deployment. Diagnosis none; fix: none proposed.
- `run-9d7f47104d` (sregym-misconfig-app-hotel-res): **success**: oracle verified recovery (sregym). Diagnosis `bad_deployment@geo`; fix: execute_rollback on geo: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.

## Methods

- Runs started without a plan (no live-eval label: scripts/demo.py or the API), in one data folder, in the order they ran.
- Rebuilt on 2026-11-07 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 1ceeb329ba37+dirty.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: approved by console-user; expired without a decision; refused: approval request expired before a decision was made.
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4: model_server_seconds sums it, and model_wait_seconds sums the part of it a request spent waiting inside the server instead of computing (server time minus Ollama's own load, prompt and generation time).

## Limitations (what these numbers do not show)

- N = 5 (sregym-misconfig-app-hotel-res 5): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is a real Kubernetes cluster (Kind, on the owner's VM): real pods, real reads and real writes, but not production traffic, and one small cluster.
- SREGym's own cluster: its hotel-reservation application and the problem SREGym's harness injected. SREGym's mitigation oracle gives the benchmark's verdict, which these records do not hold (see the notes below).
- A model's run is not exactly repeatable (random IDs in its prompts change the input), and a run on a real cluster cannot be replayed (a replay is refused against a live target): each record stands for its own run.
- Every fix that reached an approval was decided by someone other than the simulation auto-approver (each record's approvals say who and how).
- Not held out: SREGym's misconfig_app_hotel_res is a development problem. RECAP was changed between these runs because of them: after run-e08c17d46e the proposal schema offers only the restore points the evidence names (fb15190); before run-69e580846e the review fixes, including the tool descriptions of policy-sregym-hotel-v2 (4278537); after run-69e580846e a crash loop between two crashes is named as one (da260ae); after run-cb90973e56 a start-up crash that stopped is not (1ceeb32). Each record names its own code version; '+dirty' means uncommitted changes were present when the run started.
- SREGym's mitigation oracle (run-oracle.py, the benchmark's own verdict, recorded here by hand and never merged with RECAP's): run-e08c17d46e failed ('Deployment geo still using incorrect image'); run-9d7f47104d passed ('Success: True', 2026-11-07 06:29 UTC). The other three runs changed nothing on the cluster and were not graded.
- A person decided every proposal at an interactive terminal; run-6ffe22367d's request expired unanswered (its prompt kept waiting four hours past the expiry, fixed in da260ae). These runs are human-in-the-loop and not comparable with SREGym's leaderboard.
- Memory was not read in run-cb90973e56 and run-9d7f47104d (--no-memory), so the failed episodes of earlier runs of this same problem could not steer them.
