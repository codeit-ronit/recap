# Run run-c21f88c519: bad-deployment

- **Final state:** RESOLVED: oracle verified recovery (simulated environment)
- **Agent decisions:** `ollama:qwen3:8b`, origin **local_llm** (4 investigation requests in tools mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `console-user`.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `689b1af0e6aa+dirty` · prompts `prompts-v4.2`
- **Environment:** simulated (simulated: True)
- **Time:** 2026-10-21T09:38:51.940006+00:00 to 2026-10-21T09:43:34.001468+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 7 HTTP exchanges with the model server (5 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261021-093851-ollama-17f273.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2066 | 57 | 20.9 | yes |
| 2 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2440 | 46 | 9.0 | yes |
| 3 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2783 | 36 | 10.2 | yes |
| 4 | investigator | investigate | tools | `qwen3:8b` | hypothesis | 3091 | 255 | 26.8 | yes |
| 5 | controller | propose | json | `qwen3:8b` | final\_proposal | 2454 | 330 | 37.2 | yes |

- request 1: Ollama first loaded the model into memory (6.0s)

## Read-only tool calls

Chosen by the model, run by the gateway:

- `investigator` → `get_service_health(service=checkout)`
- `investigator` → `get_service_metrics(service=checkout, metric=error_rate, window_minutes=60)`
- `investigator` → `get_recent_deployments(service=checkout)`
- `investigator` → `get_k8s_like_events(service=checkout)`
- `investigator` → `get_service_logs(service=checkout, limit=50, since_minutes=60)`

Policy decisions (every call is judged before it is sent):

- 5 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: bad_deployment on `checkout` `{"version": "v2.4.1"}` (cites `ev-c21f88-001, ev-c21f88-002, ev-c21f88-003, ev-c21f88-005`)
- **Proposal (llm):** `execute_rollback` on `checkout` `{"to_version": "v2.4.0"}`. Rationale (planner text, untrusted): The error rate and latency issues in the checkout service are caused by a bug in the PricingClient.applyDiscount method introduced in the v2.4.1 deployment. The evidence shows that the error rate spiked after the deployment of v2.4.1, and the logs confirm a NullPointerException in this method. The recommended action for a bad deployment is to roll back to the last healthy version, which is v2.4.0.
- **Approval:** approved by `console-user` (answered at the console prompt: interactive terminal; answered after 175.9 s), approval mode `manual`, apr-62983d67ae, 175.9 s after the request
- **Approval decision log** (append-only):
  - 2026-10-21T09:40:37.448782+00:00 requested by `recap`: run run-c21f88c519; expires 2026-10-21T09:55:37.448782+00:00
  - 2026-10-21T09:43:33.323064+00:00 approved by `console-user`: interactive terminal; answered after 175.9 s
  - 2026-10-21T09:43:33.371656+00:00 consumed by `recap`
- **Write:** `execute_rollback` on `checkout` `{"to_version": "v2.4.0"}`: ok True; checkout: version v2.4.1 -> v2.4.0; checkout: pods replaced; under approval `apr-62983d67ae`
- **Action result:** `execute_rollback` on `checkout` → recovered
- **Oracle (`simulated-health-oracle`):** healthy before False → after True; verified True; `checkout` error rate 0.32 → 0.0

## Usage

- 5 model requests for 5 agent decisions · 12,834 input (of which 4,934 came from the server's prompt cache) / 724 output tokens, as the server reported them
- 6 tool calls (5 reads, 1 approved write)
- Cost, as the run recorded it: $0.00 - local model on this machine, no paid or remote API; 12,834 in / 724 out tokens reported by the server

## Scope and caveats

- One run of one simulated scenario (`bad-deployment`): it shows that the path works end to end, not how often the model is right.
- Memory: not read (memory off for this run)
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-reading-signals#error-rate-1`, `guide-reading-signals#latency-1`, `runbook-bad-deployment#remediation-1`, `runbook-bad-deployment#verification-1`, `runbook-memory-pressure#remediation-1`, `shop-architecture#service-level-objectives-1`
- The verified run was saved to experience memory as `ep-98da966f41` (partition `demo`).
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The proposal answer had to follow a JSON schema: an action from `execute_rollback`, `restart_service`, a listed service, and evidence IDs the run had seen.
- The recording replays offline with the same RECAP code and prompts (`prompts-v4.2`); after a prompt change a replay stops with ReplayMismatch instead of guessing.
- Wall time 282 s, of which 176 s waiting for the approval.

