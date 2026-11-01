# Real model under M4: qwen3:8b on the held-out unfamiliar incident (one agent)

> **Real model on simulated incidents. SIMULATED target environment.** N = 1: counts, not rates; no statistical claim.

- Batch `2026-10-26-real-llm-qwen3-8b-m4-unfamiliar-single` (mode `real_llm`), created 2026-10-26T05:20:58.692465+00:00
- Plan: `evals/plans/m4-comparisons.yaml`
- Providers: `ollama:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| success | 1 of 1 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds | server seconds (waited inside) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-fd18436dfe` | `m3-inventory-leak-behind-frontend` | single (investigator); single (investigator) | bm25 | on | RESOLVED | success | verified | 11 | 40,571 / 1,216 | 9,083 | 14 | 303.8 (0.0) | 300.2 | 299.8 (2.9) |

## Why each run ended as it did

- `run-fd18436dfe` (m3-inventory-leak-behind-frontend): **success**: oracle verified recovery (simulated environment). Diagnosis `resource_pressure@inventory`; fix: restart_service on inventory: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.

## Methods

- Plan evals/plans/m4-comparisons.yaml: its runs in one data folder, in order (scripts/live_eval.py, every run labelled live-eval:<run id>).
- Rebuilt on 2026-11-01 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 93c42e006003.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: approved by auto-approver[simulation].
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4: model_server_seconds sums it, and model_wait_seconds sums the part of it a request spent waiting inside the server instead of computing (server time minus Ollama's own load, prompt and generation time).

## Limitations (what these numbers do not show)

- N = 1 (m3-inventory-leak-behind-frontend 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is RECAP's SIMULATED shop, not a real cluster (a real cluster is M5).
- A model's run is not exactly repeatable (random IDs in its prompts change the input); only a replay of its recording is.
- Every fix was approved by the simulation auto-approver: these runs do not test human review.
- Planned but not in this folder: unfamiliar-parallel, unfamiliar-hybrid, bad-deployment, cross-service, memory-first, look-alike-memory-on, look-alike-memory-off, adversarial.
