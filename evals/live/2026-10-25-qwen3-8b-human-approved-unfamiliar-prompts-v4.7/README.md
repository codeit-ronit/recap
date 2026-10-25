# Run run-0c1d8fa568: m3-inventory-leak-behind-frontend

- **Final state:** RESOLVED: oracle verified recovery (simulated environment)
- **Agent decisions:** `ollama:qwen3:8b`, origin **local_llm** (13 investigation requests in tools mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `console-user`.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `0e217a11d0a5+dirty` · prompts `prompts-v4.7`
- **Environment:** simulated (simulated: True)
- **Time:** 2026-10-25T12:18:28.315653+00:00 to 2026-10-25T12:29:36.505181+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 16 HTTP exchanges with the model server (14 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261025-121828-ollama-6a5cfd.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2134 | 72 | 24.2 | yes |
| 2 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2255 | 42 | 39.8 | yes |
| 3 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2628 | 80 | 27.3 | yes |
| 4 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2531 | 60 | 25.3 | yes |
| 5 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3170 | 72 | 32.0 | yes |
| 6 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2791 | 38 | 30.0 | yes |
| 7 | telemetry\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 3919 | 424 | 62.2 | yes |
| 8 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 3284 | 258 | 79.3 | yes |
| 9 | telemetry\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 4644 | 436 | 86.7 | yes |
| 10 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 4765 | 60 | 122.9 | yes |
| 11 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 5128 | 43 | 33.1 | yes |
| 12 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 5427 | 30 | 42.7 | no |
| 13 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 5504 | 507 | 61.3 | yes |
| 14 | controller | propose | json | `qwen3:8b` | final\_proposal | 4405 | 272 | 67.4 | yes |

- request 12: dropped a repeat of mcp:get\_k8s\_like\_events(limit=5,service=inventory)
- request 12: unusable: every tool call you asked for repeats a call already observed in this run (mcp:get\_k8s\_like\_events(limit=5,service=inventory)); their results are already in the evidence above. Call a different tool, or the same tool for another service, or finish with your best-supported claim.

## Read-only tool calls

Chosen by the model, run by the gateway:

- `telemetry_investigator` → `get_service_metrics(service=frontend, metric=error_rate, window_minutes=60)`
- `telemetry_investigator` → `get_service_metrics(service=cart, metric=error_rate, window_minutes=60)`
- `infrastructure_investigator` → `get_service_health(service=frontend)`
- `infrastructure_investigator` → `get_service_health(service=cart)`
- `telemetry_investigator` → `get_service_metrics(service=frontend, metric=latency_p95_ms, window_minutes=60)`
- `telemetry_investigator` → `get_service_metrics(service=cart, metric=latency_p95_ms, window_minutes=60)`
- `infrastructure_investigator` → `get_k8s_like_events(service=frontend, limit=5)`
- `infrastructure_investigator` → `get_k8s_like_events(service=cart, limit=5)`
- `telemetry_investigator` → `get_service_logs(service=frontend, limit=50, since_minutes=60)`
- `telemetry_investigator` → `get_service_logs(service=cart, limit=50, since_minutes=60)`
- `infrastructure_investigator` → `get_recent_deployments(limit=5)`
- `infrastructure_investigator` → `list_services()`
- `infrastructure_investigator` → `get_k8s_like_events(service=checkout, limit=5)`
- `infrastructure_investigator` → `get_k8s_like_events(service=inventory, limit=5)`
- `infrastructure_investigator` → `get_recent_deployments(service=inventory)`
- `infrastructure_investigator` → `get_service_health(service=inventory)`

Policy decisions (every call is judged before it is sent):

- 14 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 2 × ALLOW, decided by `R8-read-allow` (7 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: resource_pressure on `inventory` `{}` (cites `ev-0c1d8f-r2i02`)
- **Proposal (llm):** `restart_service` on `inventory` `{}`. Rationale (planner text, untrusted): The verified root cause is resource\_pressure on the inventory service, supported by the OOMKilled events and the evidence ev-0c1d8f-r2i02. The recommended action for resource\_pressure is to restart the service, which is safe as inventory has two replicas. The restart\_service tool is appropriate here.
- **Approval:** approved by `console-user` (answered at the console prompt: interactive terminal; answered after 162.5 s), approval mode `manual`, apr-db2a9cd4d7, 162.5 s after the request
- **Approval decision log** (append-only):
  - 2026-10-25T12:26:53.185165+00:00 requested by `recap`: run run-0c1d8fa568; expires 2026-10-25T12:41:53.185165+00:00
  - 2026-10-25T12:29:35.703635+00:00 approved by `console-user`: interactive terminal; answered after 162.5 s
  - 2026-10-25T12:29:35.762573+00:00 consumed by `recap`
- **Write:** `restart_service` on `inventory` `{}`: ok True; inventory: 2 pods restarted; under approval `apr-db2a9cd4d7`
- **Action result:** `restart_service` on `inventory` → recovered
- **Oracle (`simulated-health-oracle`):** healthy before False → after True; verified True; `inventory` error rate 0.38 → 0.0

## Usage

- 14 model requests for 13 agent decisions · 52,585 input (of which 24,184 came from the server's prompt cache) / 2,394 output tokens, as the server reported them
- 17 tool calls (16 reads, 1 approved write)
- Cost, as the run recorded it: $0.00 - local model on this machine, no paid or remote API; 52,585 in / 2,394 out tokens reported by the server

## Scope and caveats

- One run of one simulated scenario (`m3-inventory-leak-behind-frontend`): it shows that the path works end to end, not how often the model is right.
- Memory: read
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-reading-signals#error-rate-1`, `runbook-bad-deployment#remediation-1`, `runbook-memory-pressure#remediation-1`, `shop-architecture#call-graph-1`, `shop-architecture#service-level-objectives-1`, `shop-architecture#services-1`
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The proposal answer had to follow a JSON schema: an action from `execute_rollback`, `restart_service`, a listed service, and evidence IDs the run had seen.
- The recording replays offline with the same RECAP code and prompts (`prompts-v4.7`); after a prompt change a replay stops with ReplayMismatch instead of guessing. This run had a parallel round: its specialists' prompts include what every agent had already observed, so a replay depends on the order they ran in and may stop with ReplayMismatch.
- Wall time 668 s, of which 163 s waiting for the approval.

