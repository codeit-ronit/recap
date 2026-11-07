# Run run-34a3580d2f: kind-bad-release

- **Final state:** RESOLVED: oracle verified recovery (kind)
- **Agent decisions:** `ollama-vm:qwen3:8b`, origin **local_llm** (7 investigation requests in tools mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `operator:local-operator`.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `3c654d945bc9` · prompts `prompts-v4.7`
- **Environment:** kind (simulated: False)
- **Time:** 2026-11-07T09:11:25.433110+00:00 to 2026-11-07T09:29:24.497433+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 10 HTTP exchanges with the model server (8 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261107-091044-ollama-vm-551d62.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2243 | 56 | 241.2 | yes |
| 2 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2134 | 72 | 285.2 | yes |
| 3 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2529 | 62 | 110.3 | yes |
| 4 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2620 | 74 | 141.1 | yes |
| 5 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3144 | 42 | 157.9 | yes |
| 6 | telemetry\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 3260 | 291 | 258.1 | yes |
| 7 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 3422 | 274 | 175.6 | yes |
| 8 | controller | propose | json | `qwen3:8b` | final\_proposal | 3540 | 318 | 207.3 | yes |

- request 1: Ollama first loaded the model into memory (43.0s)
- request 2: Ollama first loaded the model into memory (43.0s)

## Read-only tool calls

Chosen by the model, run by the gateway:

- `infrastructure_investigator` → `get_recent_deployments(service=frontend, limit=5)`
- `infrastructure_investigator` → `get_recent_deployments(service=backend, limit=5)`
- `telemetry_investigator` → `get_service_metrics(service=frontend, metric=error_rate, window_minutes=60)`
- `telemetry_investigator` → `get_service_metrics(service=backend, metric=error_rate, window_minutes=60)`
- `infrastructure_investigator` → `get_k8s_like_events(service=frontend, limit=10)`
- `infrastructure_investigator` → `get_k8s_like_events(service=backend, limit=10)`
- `telemetry_investigator` → `get_service_logs(service=frontend, limit=50, since_minutes=120)`
- `telemetry_investigator` → `get_service_logs(service=backend, limit=50, since_minutes=120)`
- `infrastructure_investigator` → `get_service_health(service=frontend)`
- `infrastructure_investigator` → `get_service_health(service=backend)`

Policy decisions (every call is judged before it is sent):

- 10 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: bad_deployment on `backend` `{"version": "v1.1.0"}` (cites `ev-34a358-r1i02`)
- **Proposal (llm):** `execute_rollback` on `backend` `{"to_version": "v1.0.0"}`. Rationale (planner text, untrusted): The verified root cause is a bad deployment of the backend service, specifically version v1.1.0, which introduced errors causing HTTP 500 responses. The evidence shows that the error rate spiked shortly after the deployment, and the logs confirm a KeyError in the backend service. The recommended action for a bad deployment is to roll back to a previous version. The rollback should be executed to the last healthy revision, which is v1.0.0.
- **Approval:** approved by `operator:local-operator` (a signed-in operator, through the API or the dashboard), approval mode `manual`, apr-2104a33503, 152.0 s after the request
- **Approval decision log** (append-only):
  - 2026-11-07T09:26:24.284643+00:00 requested by `recap`: run run-34a3580d2f; expires 2026-11-07T09:41:24.284643+00:00
  - 2026-11-07T09:28:56.318024+00:00 approved by `operator:local-operator`: dashboard or API; typed the target service's name; answered 152.0 s after the request
  - 2026-11-07T09:28:59.842702+00:00 consumed by `recap`
- **Write:** `execute_rollback` on `backend` `{"to_version": "v1.0.0"}`: ok True; backend generation 18 -> 19; backend pod template image: recap-demo/backend:v1.0.0; under approval `apr-2104a33503`
- **Action result:** `execute_rollback` on `backend` → recovered
- **Oracle (`kind-health-oracle`):** healthy before False → after True; verified True; `backend` error rate 0.3 → 0.0

## Usage

- 8 model requests for 8 agent decisions · 22,892 input (of which 8,423 came from the server's prompt cache) / 1,189 output tokens, as the server reported them
- 11 tool calls (10 reads, 1 approved write)
- Cost, as the run recorded it: $0.00 per request - self-hosted open-weight model (this machine or the owner's own VM), no paid API; 22,892 in / 1,189 out tokens reported by the server

## Scope and caveats

- One run of one scenario (`kind-bad-release`) on a real cluster (`kind`: real faults, reads and writes, not production): it shows that the path works end to end, not how often the model is right.
- Memory: not read (memory off for this run)
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-reading-signals#error-rate-1`, `runbook-bad-deployment#remediation-1`, `runbook-memory-pressure#remediation-1`, `runbook-memory-pressure#symptoms-1`, `shop-architecture#call-graph-1`, `shop-architecture#service-level-objectives-1`
- The verified run was saved to experience memory as `ep-9f3b0112ae` (partition `live`).
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The proposal answer had to follow a JSON schema: an action from `execute_rollback`, `restart_service`, a listed service, and evidence IDs the run had seen.
- The recording keeps the model's answers as its server sent them, as evidence; it cannot be replayed: a replay is refused against a live target, and its prompts hold the real cluster's answers, which the simulator would not give.
- Wall time 1079 s, of which 152 s waiting for the approval.

