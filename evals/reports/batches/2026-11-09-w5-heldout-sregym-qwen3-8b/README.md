# W5 held-out SREGym evaluation: qwen3:8b, prompts-v4.7 (3 of 5 frozen problems attempted)

> **Real model on a real Kubernetes cluster. REAL target: a Kubernetes cluster (sregym), not production.** N = 3: counts, not rates; no statistical claim.

- Batch `2026-11-09-w5-heldout-sregym-qwen3-8b` (mode `real_kubernetes`), created 2026-11-09T08:37:19.416255+00:00
- Plan: none (see methods)
- Providers: `ollama-vm:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| cancelled | 1 of 3 |
| missed_crucial_tool | 1 of 3 |
| wrong_mechanism | 1 of 3 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds | server seconds (waited inside) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-7bc5159b6d` | `sregym-w5-1` | parallel (telemetry + infrastructure); single (investigator) | bm25 | on | FAILED | wrong_mechanism | not verified | 13 | 57,253 / 1,318 | 18,621 | 21 | 3135.9 (0.0) | 4536.0 | 4531.2 (527.8) |
| `run-db560f6568` | `sregym-w5-2` | single (investigator); parallel (telemetry + infrastructure) | bm25 | on | FAILED | missed_crucial_tool | not verified | 10 | 45,255 / 759 | 14,063 | 10 | 2069.8 (0.0) | 2059.9 | 2058.6 (214.5) |
| `run-62f9029f00` | `sregym-w5-3` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | cancelled | not verified | 9 | 33,593 / 1,014 | 13,293 | 12 | 1668.0 (0.0) | 2639.8 | 2638.1 (49.6) |

## Why each run ended as it did

- `run-7bc5159b6d` (sregym-w5-1): **wrong_mechanism**: the claims on frontend named resource_pressure, not bad_deployment. Diagnosis none; fix: none proposed.
- `run-db560f6568` (sregym-w5-2): **missed_crucial_tool**: no tool call named aux-service and no recorded observation showed it; the calls named mongodb-rate, rate. Diagnosis none; fix: none proposed.
- `run-62f9029f00` (sregym-w5-3): **cancelled**: the run was cancelled before it finished. Diagnosis none; fix: none proposed.

## Methods

- Runs started without a plan (no live-eval label: scripts/demo.py or the API), in one data folder, in the order they ran.
- Rebuilt on 2026-11-09 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 042e0db212d1+dirty.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: none were requested.
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4: model_server_seconds sums it, and model_wait_seconds sums the part of it a request spent waiting inside the server instead of computing (server time minus Ollama's own load, prompt and generation time).

## Limitations (what these numbers do not show)

- N = 3 (sregym-w5-1 1, sregym-w5-2 1, sregym-w5-3 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is a real Kubernetes cluster (Kind, on the owner's VM): real pods, real reads and real writes, but not production traffic, and one small cluster.
- SREGym's own cluster: its hotel-reservation application and the problem SREGym's harness injected. SREGym's own grader gives the benchmark's verdict, kept apart from RECAP's success: a record holds it only when it was recorded (benchmark_verdict).
- A model's run is not exactly repeatable (random IDs in its prompts change the input), and a run on a real cluster cannot be replayed (a replay is refused against a live target): each record stands for its own run.
- No fix reached an approval, so no run tested the approval step.
- Held out and blind: the five problems were chosen from SREGym's problem names alone by a rule written down first, the scenario files held no answer during the runs, and everything that decides RECAP's behaviour was frozen and checked before each run (evals/frozen/w5-heldout-sregym.yaml). The answers in the scenario files were written after the evaluation, from SREGym's own definitions; a build before them put the same three runs in insufficient_evidence (2) and cancelled (1).
- Stopped after 3 of the 5 frozen problems, by the protocol's stop condition and the owner's decision: the self-hosted model failed for the same infrastructure reason in sregym-w5-1 and sregym-w5-2 (CPU-only inference too slow for two parallel specialists: a specialist ran out of time after 2520 s in the first, and a model request did not answer within 900 s in the second). sregym-w5-4 and sregym-w5-5 were never attempted: their scenario files stay blind and their definitions unread.
- sregym-w5-3 was cancelled by the operator (Ctrl-C) 28 minutes in, during its second investigation round, after every hypothesis of its first round had been contradicted. It is reported as cancelled and was not repeated; SREGym graded it about 2.5 minutes after the cancellation (the first grade was interrupted with it).
- No run could have succeeded: each injected fault changes a Deployment's spec but not its image (shared storage and replicas, a liveness probe, an init container), so RECAP's rollback finds no earlier revision with another image and a restart re-creates the same pods. A correct run could at best have diagnosed the fault and handed it to a person. Choosing by problem name alone (so that no answer was read) could not tell; these runs measure diagnosis and safety, not repair.
- sregym-w5-2's fault is on aux-service, a workload SREGym adds to the namespace outside the hotel-reservation services this profile allowlists: RECAP's tools could not observe it, so its category reflects the profile's scope rather than a choice of the agents.
- Latency: on the VM's 8 vCPUs (no GPU) a model request took 103 to 616 s (median 259 s over 32 requests), longer when two specialists shared the one server; the runs took 52, 35 and 28 (cancelled) minutes.
