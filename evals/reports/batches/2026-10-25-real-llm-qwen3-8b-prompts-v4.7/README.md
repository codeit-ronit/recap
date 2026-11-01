# M3 re-runs after prompts-v4.7: qwen3:8b (local)

> **Real model on simulated incidents. SIMULATED target environment.** N = 2: counts, not rates; no statistical claim.

- Batch `2026-10-25-real-llm-qwen3-8b-prompts-v4.7` (mode `real_llm`), created 2026-10-25T11:09:41.298467+00:00
- Plan: none (see methods)
- Providers: `ollama:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| success | 1 of 2 |
| wrong_mechanism | 1 of 2 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds | server seconds (waited inside) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-6de4ee6acc` | `m3-inventory-leak-behind-frontend` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | RESOLVED | success | verified | 18 | 74,935 / 2,182 | 19,893 | 25 | 1379.8 (0.0) | 1222.7 | not recorded |
| `run-33427c4450` | `ambiguous-incident` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | wrong_mechanism | not verified | 16 | 60,861 / 1,779 | 12,884 | 18 | 567.2 (0.0) | 970.7 | not recorded |

## Why each run ended as it did

- `run-6de4ee6acc` (m3-inventory-leak-behind-frontend): **success**: oracle verified recovery (simulated environment). Diagnosis `resource_pressure@inventory`; fix: restart_service on inventory: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.
- `run-33427c4450` (ambiguous-incident): **wrong_mechanism**: the claims on payment named resource_pressure, not bad_deployment. Diagnosis none; fix: none proposed.

## Methods

- Runs of a live-evaluation plan that is not in the repository (labels live-eval:unfamiliar-v47, live-eval:cross-service-v47), in one data folder, in the order they ran; each record shows its own scenario, routing and retrieval.
- Rebuilt on 2026-11-01 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 93c42e006003.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: approved by auto-approver[simulation].
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4: model_server_seconds sums it, and model_wait_seconds sums the part of it a request spent waiting inside the server instead of computing (server time minus Ollama's own load, prompt and generation time).

## Limitations (what these numbers do not show)

- N = 2 (m3-inventory-leak-behind-frontend 1, ambiguous-incident 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is RECAP's SIMULATED shop, not a real cluster (a real cluster is M5).
- A model's run is not exactly repeatable (random IDs in its prompts change the input); only a replay of its recording is.
- Every fix was approved by the simulation auto-approver: these runs do not test human review.
