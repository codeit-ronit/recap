# Run run-047aac6eb0: kind-bad-release

- **Final state:** RESOLVED: oracle verified recovery (kind)
- **Agent decisions:** `ollama-vm:qwen3:8b`, origin **local_llm** (7 investigation requests in tools mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `console-user`.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `525c28d1e5d9` · prompts `prompts-v4.7`
- **Environment:** kind (simulated: False)
- **Time:** 2026-11-03T12:51:41.282914+00:00 to 2026-11-03T13:13:24.388821+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 10 HTTP exchanges with the model server (8 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261103-124819-ollama-vm-7a1b6a.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2885 | 42 | 263.8 | yes |
| 2 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2776 | 72 | 342.2 | yes |
| 3 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3143 | 56 | 170.0 | yes |
| 4 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3257 | 76 | 188.2 | yes |
| 5 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3423 | 62 | 212.2 | yes |
| 6 | telemetry\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 3891 | 291 | 329.6 | yes |
| 7 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 3988 | 306 | 221.8 | yes |
| 8 | controller | propose | json | `qwen3:8b` | final\_proposal | 4371 | 299 | 247.7 | yes |

- request 1: Ollama first loaded the model into memory (8.8s)
- request 2: Ollama first loaded the model into memory (8.8s)

## Read-only tool calls

Chosen by the model, run by the gateway:

- `infrastructure_investigator` → `get_service_health(service=frontend)`
- `infrastructure_investigator` → `get_service_health(service=backend)`
- `telemetry_investigator` → `get_service_metrics(service=frontend, metric=error_rate, window_minutes=60)`
- `telemetry_investigator` → `get_service_metrics(service=backend, metric=error_rate, window_minutes=60)`
- `infrastructure_investigator` → `get_recent_deployments(service=frontend, limit=5)`
- `infrastructure_investigator` → `get_recent_deployments(service=backend, limit=5)`
- `telemetry_investigator` → `get_service_logs(service=frontend, limit=100, since_minutes=240)`
- `telemetry_investigator` → `get_service_logs(service=backend, limit=100, since_minutes=240)`
- `infrastructure_investigator` → `get_k8s_like_events(service=frontend, limit=10)`
- `infrastructure_investigator` → `get_k8s_like_events(service=backend, limit=10)`

Policy decisions (every call is judged before it is sent):

- 10 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: bad_deployment on `backend` `{"version": "v1.1.0"}` (cites `ev-047aac-r1i04, ev-047aac-r1i06`)
- **Proposal (llm):** `execute_rollback` on `backend` `{"to_version": "v1.0.0"}`. Rationale (planner text, untrusted): The verified root cause is a bad deployment of the backend service, which introduced errors causing the frontend to fail. The evidence shows that the backend service's error rate spiked after the deployment of version v1.1.0, and the logs confirm a KeyError in the backend code. The recommended action for a bad deployment is to roll back to the previous healthy version.
- **Approval:** approved by `console-user` (answered at the console prompt: interactive terminal; typed the target service's name; answered after 167.8 s; login name kept in the local decision log), approval mode `manual`, apr-84ec57cc5a, 167.8 s after the request
- **Approval decision log** (append-only):
  - 2026-11-03T13:10:20.513252+00:00 requested by `recap`: run run-047aac6eb0; expires 2026-11-03T13:25:20.513252+00:00
  - 2026-11-03T13:13:08.296224+00:00 approved by `console-user`: interactive terminal; typed the target service's name; answered after 167.8 s; login name kept in the local decision log
  - 2026-11-03T13:13:11.409260+00:00 consumed by `recap`
- **Write:** `execute_rollback` on `backend` `{"to_version": "v1.0.0"}`: ok True; backend generation 14 -> 15; backend pod template image: recap-demo/backend:v1.0.0; under approval `apr-84ec57cc5a`
- **Action result:** `execute_rollback` on `backend` → recovered
- **Oracle (`kind-health-oracle`):** healthy before False → after True; verified True; `backend` error rate 0.3 → 0.0

## Usage

- 8 model requests for 8 agent decisions · 27,734 input (of which 8,381 came from the server's prompt cache) / 1,204 output tokens, as the server reported them
- 11 tool calls (10 reads, 1 approved write)
- Cost, as the run recorded it: $0.00 per request - self-hosted open-weight model (this machine or the owner's own VM), no paid API; 27,734 in / 1,204 out tokens reported by the server

## Scope and caveats

- One run of one scenario (`kind-bad-release`) on a real cluster (`kind`: real faults, reads and writes, not production): it shows that the path works end to end, not how often the model is right.
- Memory: read
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-reading-signals#error-rate-1`, `runbook-bad-deployment#remediation-1`, `runbook-memory-pressure#remediation-1`, `runbook-memory-pressure#symptoms-1`, `shop-architecture#call-graph-1`, `shop-architecture#service-level-objectives-1`
- The verified run was saved to experience memory as `ep-aa92fd4659` (partition `live`).
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The proposal answer had to follow a JSON schema: an action from `execute_rollback`, `restart_service`, a listed service, and evidence IDs the run had seen.
- The recording keeps the model's answers as its server sent them, as evidence; it cannot be replayed: a replay is refused against a live target, and its prompts hold the real cluster's answers, which the simulator would not give.
- Wall time 1303 s, of which 168 s waiting for the approval.

