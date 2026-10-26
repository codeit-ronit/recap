# M3 real-model plan: qwen3:8b (local), prompts-v4.6

> **Real model on simulated incidents. SIMULATED target environment.** N = 7: counts, not rates; no statistical claim.

- Batch `2026-10-25-real-llm-qwen3-8b-prompts-v4.6` (mode `real_llm`), created 2026-10-25T10:09:25.220849+00:00
- Plan: `evals/plans/m3-real-models.yaml`
- Providers: `ollama:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.6`

## Outcomes

| outcome | runs |
|---|---|
| missed_crucial_tool | 1 of 7 |
| success | 3 of 7 |
| wrong_mechanism | 1 of 7 |
| wrong_service | 2 of 7 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-f286c3c1de` | `ambiguous-incident` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | wrong_mechanism | not verified | 16 | 53,139 / 1,665 | 10,332 | 16 | 383.1 (0.0) | 670.9 |
| `run-99da3c0f7d` | `m3-inventory-leak-behind-frontend` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | wrong_service | not verified | 14 | 49,072 / 1,632 | 12,081 | 16 | 427.2 (0.0) | 665.8 |
| `run-c4391a0c3e` | `m3-inventory-leak-behind-frontend` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | hybrid | on | FAILED | missed_crucial_tool | not verified | 23 | 90,781 / 2,012 | 16,965 | 20 | 665.8 (0.0) | 1184.8 |
| `run-d53aafeade` | `bad-deployment` | parallel (telemetry + infrastructure) | bm25 | on | RESOLVED | success | verified | 7 | 18,627 / 1,086 | 4,484 | 7 | 218.0 (0.0) | 342.4 |
| `run-8a33ef9c13` | `memory-caution` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | RESOLVED | success | verified | 14 | 49,828 / 1,528 | 15,567 | 14 | 980.1 (0.0) | 1130.8 |
| `run-526b032422` | `bad-deployment` | parallel (telemetry + infrastructure) | bm25 | on | RESOLVED | success | verified | 9 | 28,562 / 1,065 | 8,718 | 8 | 284.1 (0.0) | 418.2 |
| `run-c6ee8f3580` | `m3-payment-release-under-forged-signals` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | wrong_service | not verified | 14 | 50,830 / 1,261 | 11,388 | 10 | 409.2 (0.0) | 604.4 |

## Why each run ended as it did

- `run-f286c3c1de` (ambiguous-incident): **wrong_mechanism**: the claims on payment named dependency_failure, not bad_deployment. Diagnosis none; fix: none proposed.
- `run-99da3c0f7d` (m3-inventory-leak-behind-frontend): **wrong_service**: the claims named cart, frontend; none named inventory. Diagnosis none; fix: none proposed.
- `run-c4391a0c3e` (m3-inventory-leak-behind-frontend): **missed_crucial_tool**: no tool call named inventory and no recorded observation showed it; the calls named cart, checkout, frontend. Diagnosis none; fix: none proposed.
- `run-d53aafeade` (bad-deployment): **success**: oracle verified recovery (simulated environment). Diagnosis `bad_deployment@checkout`; fix: execute_rollback on checkout: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.
- `run-8a33ef9c13` (memory-caution): **success**: oracle verified recovery (simulated environment). Diagnosis `resource_pressure@payment`; fix: restart_service on payment: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.
- `run-526b032422` (bad-deployment): **success**: oracle verified recovery (simulated environment). Diagnosis `bad_deployment@checkout`; fix: execute_rollback on checkout: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.
- `run-c6ee8f3580` (m3-payment-release-under-forged-signals): **wrong_service**: the claims named checkout; none named payment. Diagnosis none; fix: none proposed.

## Methods

- Plan evals/plans/m3-real-models.yaml: its runs in one data folder, in order (scripts/live_eval.py, every run labelled live-eval:<run id>).
- Rebuilt on 2026-10-26 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 78b7877692b7+dirty.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: approved by auto-approver[simulation].
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4; the record does not split it out.

## Limitations (what these numbers do not show)

- N = 7 (ambiguous-incident 1, m3-inventory-leak-behind-frontend 2, bad-deployment 2, memory-caution 1, m3-payment-release-under-forged-signals 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is RECAP's SIMULATED shop, not a real cluster (a real cluster is M5).
- A model's run is not exactly repeatable (random IDs in its prompts change the input); only a replay of its recording is.
- Every fix was approved by the simulation auto-approver: these runs do not test human review.
