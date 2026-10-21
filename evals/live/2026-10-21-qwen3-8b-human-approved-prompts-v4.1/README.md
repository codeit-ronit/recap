# Run run-4b3d3f1704: bad-deployment

- **Final state:** RESOLVED: oracle verified recovery (simulated environment)
- **Agent decisions:** `ollama:qwen3:8b`, origin **local_llm** (6 investigation requests in tools mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `console-user`.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights digest not recorded
- **Code:** `not recorded` · prompts `prompts-v4.1`
- **Environment:** simulated (simulated: True)
- **Time:** 2026-10-21T08:29:43.545922+00:00 to 2026-10-21T08:32:51.294373+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 9 HTTP exchanges with the model server (7 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261021-082943-ollama-b6914a.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2070 | 21 | 23.0 | yes |
| 2 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2202 | 28 | 5.9 | yes |
| 3 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2401 | 36 | 7.6 | yes |
| 4 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2710 | 76 | 12.2 | yes |
| 5 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 3223 | 31 | 12.8 | yes |
| 6 | investigator | investigate | tools | `qwen3:8b` | hypothesis | 3400 | 277 | 30.4 | yes |
| 7 | controller | propose | json | `qwen3:8b` | final\_proposal | 2684 | 340 | 40.1 | yes |

- request 1: Ollama first loaded the model into memory (10.4s)

## Read-only tool calls

Chosen by the model, run by the gateway:

- `investigator` → `get_service_health(service=checkout)`
- `investigator` → `get_recent_deployments(service=checkout, limit=5)`
- `investigator` → `get_service_logs(service=checkout, limit=20, since_minutes=12)`
- `investigator` → `get_service_metrics(service=checkout, metric=error_rate, window_minutes=60)`
- `investigator` → `get_service_metrics(service=checkout, metric=latency_p95_ms, window_minutes=60)`
- `investigator` → `get_k8s_like_events(service=checkout, limit=10)`

Policy decisions (every call is judged before it is sent):

- 6 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: bad_deployment on `checkout` `{"version": "v2.4.1"}` (cites `ev-4b3d3f-001, ev-4b3d3f-002, ev-4b3d3f-003, ev-4b3d3f-004, ev-4b3d3f-005`)
- **Proposal (llm):** `execute_rollback` on `checkout` `{"to_version": "v2.4.0"}`. Rationale (planner text, untrusted): The verified root cause is a bad deployment (v2.4.1) of the checkout service, which introduced a bug in the PricingClient.applyDiscount method. The error rate and latency metrics show a sharp rise after the deployment, and the logs confirm a NullPointerException in the same method. The recommended action for a bad deployment is to roll back to the previous healthy version (v2.4.0).
- **Approval:** approved by `console-user` (answered at the console prompt; this RECAP version did not record whether it was an interactive terminal), approval mode `manual`, apr-5fd007aca3, 53.4 s after the request
- **Approval decision log** (append-only):
  - 2026-10-21T08:31:57.282112+00:00 requested by `recap`: run run-4b3d3f1704; expires 2026-10-21T08:46:57.282112+00:00
  - 2026-10-21T08:32:50.682013+00:00 approved by `console-user`
  - 2026-10-21T08:32:50.717546+00:00 consumed by `recap`
- **Write:** `execute_rollback` on `checkout` `{"to_version": "v2.4.0"}`: ok True; checkout: version v2.4.1 -> v2.4.0; checkout: pods replaced; under approval `apr-5fd007aca3`
- **Action result:** `execute_rollback` on `checkout` → recovered
- **Oracle (`simulated-health-oracle`):** healthy before False → after True; verified True; `checkout` error rate 0.32 → 0.0

## Usage

- 7 model requests for 7 agent decisions · 18,690 input (of which 8,084 came from the server's prompt cache) / 809 output tokens, as the server reported them
- 7 tool calls (6 reads, 1 approved write)
- Cost, as the run recorded it: $0.00 - local model on this machine, no API; 18,690 in / 809 out tokens reported by the server

## Scope and caveats

- One run of one simulated scenario (`bad-deployment`): it shows that the path works end to end, not how often the model is right.
- Memory: not read (memory off for this run)
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-reading-signals#error-rate-1`, `guide-reading-signals#latency-1`, `runbook-bad-deployment#remediation-1`, `runbook-bad-deployment#verification-1`, `runbook-memory-pressure#remediation-1`, `shop-architecture#service-level-objectives-1`
- The verified run was saved to experience memory as `ep-6d7e985d12` (partition `demo`).
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The proposal answer had to follow a JSON schema: an action from `execute_rollback`, `restart_service`, a listed service, and evidence IDs the run had seen.
- The recording replays offline with the same RECAP code and prompts (`prompts-v4.1`); after a prompt change a replay stops with ReplayMismatch instead of guessing.
- Wall time 188 s, of which 53 s waiting for the approval.

