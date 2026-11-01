# Offline replay of the committed real-model recordings

> **Offline replay of recorded real-model runs (no model is called). SIMULATED target environment.** N = 2: counts, not rates; no statistical claim.

- Batch `20261101-201055-offline-replay-91203b` (mode `offline_replay`), created 2026-11-01T20:10:55.896037+00:00
- Plan: `fixtures/recordings/MANIFEST.json`
- Providers: `replay:ollama:qwen3:8b`; models: `qwen3:8b`; prompts: `prompts-v4.7`

## Outcomes

| outcome | runs |
|---|---|
| success | 2 of 2 |

## Runs

| run | scenario | rounds (agents) | retrieval | memory | state | outcome | oracle | model requests | tokens in / out (reported) | est. tokens | tool calls | run seconds (approval wait) | model seconds | server seconds (waited inside) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `run-4dac1ec94d` | `m3-inventory-leak-behind-frontend` | parallel (telemetry + infrastructure); parallel (telemetry + infrastructure) | bm25 | on | RESOLVED | success | verified | 0 | not reported | 11,800 | 17 | 2.2 (0.0) | - | not recorded |
| `run-03d48739cd` | `bad-deployment` | single (investigator) | bm25 | off | RESOLVED | success | verified | 0 | not reported | 3,836 | 7 | 1.6 (0.0) | - | not recorded |

## Why each run ended as it did

- `run-4dac1ec94d` (m3-inventory-leak-behind-frontend): **success**: oracle verified recovery (simulated environment). Diagnosis `resource_pressure@inventory`; fix: restart_service on inventory: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.
- `run-03d48739cd` (bad-deployment): **success**: oracle verified recovery (simulated environment). Diagnosis `bad_deployment@checkout`; fix: execute_rollback on checkout: APPROVAL_REQUIRED (R11-reversible-approval); evidence check passed; executed.

## Methods

- Each recording in the manifest was checked against its pinned SHA-256, then replayed with scripts/eval_replay.py through RECAP's whole path (controller, MCP tool server, gateway, simulator, verifier, approval, oracle); only the model's answers came from the recording. No model was called; $0.
- Each replay ran in a fresh data folder (a replay reads memory as its recording found it: empty), with the routing, memory and retrieval its manifest entry names, over the shipped runbooks only.
- Approvals: the simulation auto-approver stood in for the original approver (the manifest names them); the approval comes after the model's last answer, so it changes nothing the model saw.
- Model requests, reported tokens and model_seconds are the replay's own: a replay sends no request, so they are 0 or empty. The recorded run's own counts are in its recording (fixtures/recordings/) and its manifest entry.
- Each record is built from the replay's own record and trace (evals/categorize.py): success only when the independent oracle verified the recovery; a replay that left its recording (ReplayMismatch) is category error.
- RECAP code 93c42e006003 ('+dirty': with uncommitted changes).

## Limitations (what these numbers do not show)

- N = 2 recordings: a replay shows that recorded decisions still pass RECAP's current code. It is not a new sample of any model, and its outcome says nothing about how often the model would decide the same way.
- Each recording is one run of one model on the SIMULATED shop, not a real cluster.
- A replay cannot show what a model would do with a changed prompt: a changed prompt makes the replay stop (ReplayMismatch), and the recording must be made again with a real model.
