# Owner-approved run: qwen3:8b (local), prompts-v4.7 (the M3 deliverable)

> **Real model on simulated incidents. SIMULATED target environment.** N = 1: counts, not rates; no statistical claim.

- Batch `2026-10-25-real-llm-qwen3-8b-prompts-v4.7-owner-approved` (mode `real_llm`), created 2026-10-25T12:18:28.315633+00:00
- Plan: none (see methods)
- Providers: `ollama:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| success | 1 of 1 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-0c1d8fa568` | `m3-inventory-leak-behind-frontend` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | RESOLVED | success | verified | 14 | 52,585 / 2,394 | 11,800 | 17 | 668.0 (162.5) | 734.3 |

## Why each run ended as it did

- `run-0c1d8fa568` (m3-inventory-leak-behind-frontend): **success**: oracle verified recovery (simulated environment). Diagnosis `resource_pressure@inventory`; fix: restart_service on inventory: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.

## Methods

- Runs started without a plan (no live-eval label: scripts/demo.py or the API), in one data folder, in the order they ran.
- Rebuilt on 2026-10-25 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 7d004a45f5ae+dirty.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: approved by console-user.
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4; the record does not split it out.

## Limitations (what these numbers do not show)

- N = 1 (m3-inventory-leak-behind-frontend 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is RECAP's SIMULATED shop, not a real cluster (a real cluster is M5).
- A model's run is not exactly repeatable (random IDs in its prompts change the input); only a replay of its recording is.
- Every fix that reached an approval was decided by someone other than the simulation auto-approver (each record's approvals say who and how).
