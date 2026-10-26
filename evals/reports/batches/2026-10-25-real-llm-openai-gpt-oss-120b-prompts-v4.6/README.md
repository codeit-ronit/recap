# M3 free-plan runs: openai/gpt-oss-120b (Groq Free), prompts-v4.6

> **Real model on simulated incidents. SIMULATED target environment.** N = 2: counts, not rates; no statistical claim.

- Batch `2026-10-25-real-llm-openai-gpt-oss-120b-prompts-v4.6` (mode `real_llm`), created 2026-10-25T10:09:25.217410+00:00
- Plan: `evals/plans/m3-groq-free.yaml`
- Providers: `groq:openai/gpt-oss-120b`; models: `openai/gpt-oss-120b`; prompts: `prompts-v4.6`

## Outcomes

| outcome | runs |
|---|---|
| insufficient_evidence | 1 of 2 |
| quota_cap | 1 of 2 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-70fe283152` | `m3-inventory-leak-behind-frontend` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | FAILED | insufficient_evidence | not verified | 7 | 16,701 / 1,842 | 3,417 | 4 | 243.9 (0.0) | 8.6 |
| `run-7ea7004c6e` | `m3-payment-release-under-forged-signals` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | QUOTA_EXHAUSTED | quota_cap | not verified | 6 | 16,539 / 1,781 | 3,566 | 6 | 241.9 (0.0) | 7.5 |

## Why each run ended as it did

- `run-70fe283152` (m3-inventory-leak-behind-frontend): **insufficient_evidence**: the claims traced the failure to inventory, but none concluded inventory's own fault. Diagnosis none; fix: none proposed.
- `run-7ea7004c6e` (m3-payment-release-under-forged-signals): **quota_cap**: deferred: budget exhausted: provider_quota (api.groq.com: RECAP's per-minute cap is full for 60s). Diagnosis none; fix: none proposed.

## Methods

- Plan evals/plans/m3-groq-free.yaml: its runs in one data folder, in order (scripts/live_eval.py, every run labelled live-eval:<run id>).
- Rebuilt on 2026-10-26 from the runs' own records and traces with scripts/live_eval.py --from-data (no model was called); categories by evals/categorize.py at code 78b7877692b7+dirty.
- Each record is built from the run's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; any other end carries one failure category, judged against the scenario's predeclared answer, which the model never sees.
- Approvals: none were requested.
- Tokens in / out are the counts the model server reported; est. tokens is RECAP's own estimate of the context it built. Cost: $0 (a local model, or a free plan checked before every request).
- model_seconds sums each request's duration as RECAP measured it. When two specialists ask one local server that answers one request at a time, a request waits in its queue, so the sum can exceed the run's own time. The trace's llm.call events carry the server's own time (server_ms, Ollama's total_duration) for runs made since M4; the record does not split it out.

## Limitations (what these numbers do not show)

- N = 2 (m3-inventory-leak-behind-frontend 1, m3-payment-release-under-forged-signals 1): each run is one sample of one configuration. These are counts, not success rates, and no statistical claim is made.
- The target is RECAP's SIMULATED shop, not a real cluster (a real cluster is M5).
- A model's run is not exactly repeatable (random IDs in its prompts change the input); only a replay of its recording is.
- No fix reached an approval, so no run tested the approval step.
