# Real model under M4: qwen3:8b on the cross-service incident (parallel specialists)

> **Real model on simulated incidents. SIMULATED target environment.** N = 1: counts, not rates; no statistical claim.

- Batch `2026-10-26-real-llm-qwen3-8b-m4-cross-service` (mode `real_llm`), created 2026-10-26T05:07:48.054110+00:00
- Plan: `evals/plans/m4-comparisons.yaml`
- Providers: `ollama:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| wrong_mechanism | 1 of 1 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds | server seconds (waited inside) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-602b88cbf1` | `ambiguous-incident` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | wrong_mechanism | not verified | 15 | 54,945 / 1,813 | 12,318 | 18 | 473.9 (0.0) | 837.9 | 837.1 (361.8) |

## Why each run ended as it did

- `run-602b88cbf1` (ambiguous-incident): **wrong_mechanism**: the claims on payment named resource_pressure, not bad_deployment. Diagnosis none; fix: none proposed.

## Methods

- Plan evals/plans/m4-comparisons.yaml: its runs in one data folder, in order (scripts/live_eval.py, every run labelled live-eval:<run id>).
- Rebuilt on 2026-11-01 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 93c42e006003.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: none were requested.
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4: model_server_seconds sums it, and model_wait_seconds sums the part of it a request spent waiting inside the server instead of computing (server time minus Ollama's own load, prompt and generation time).

## Limitations (what these numbers do not show)

- N = 1 (ambiguous-incident 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is RECAP's SIMULATED shop, not a real cluster (a real cluster is M5).
- A model's run is not exactly repeatable (random IDs in its prompts change the input); only a replay of its recording is.
- No fix reached an approval, so no run tested the approval step.
- Planned but not in this folder: unfamiliar-single, unfamiliar-parallel, unfamiliar-hybrid, bad-deployment, memory-first, look-alike-memory-on, look-alike-memory-off, adversarial.
