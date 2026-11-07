# Run run-9d7f47104d: sregym-misconfig-app-hotel-res

- **Final state:** RESOLVED: oracle verified recovery (sregym)
- **Agent decisions:** `ollama-vm:qwen3:8b`, origin **local_llm** (4 investigation requests in tools mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `console-user`.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `1ceeb329ba37` · prompts `prompts-v4.7`
- **Environment:** sregym (simulated: False)
- **Time:** 2026-11-07T05:44:36.891688+00:00 to 2026-11-07T06:00:07.275301+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 7 HTTP exchanges with the model server (5 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261107-054426-ollama-vm-cc678a.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2999 | 67 | 131.1 | yes |
| 2 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 3887 | 28 | 99.3 | yes |
| 3 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 4080 | 36 | 108.9 | yes |
| 4 | investigator | investigate | tools | `qwen3:8b` | hypothesis | 4300 | 225 | 167.0 | yes |
| 5 | controller | propose | json | `qwen3:8b` | final\_proposal | 3171 | 380 | 222.1 | yes |

## Read-only tool calls

Chosen by the model, run by the gateway:

- `investigator` → `get_k8s_like_events(service=geo, limit=10)`
- `investigator` → `get_service_logs(service=geo, limit=50, since_minutes=10)`
- `investigator` → `get_recent_deployments(service=geo, limit=5)`
- `investigator` → `get_service_metrics(service=geo, metric=restarts, window_minutes=10)`

Policy decisions (every call is judged before it is sent):

- 4 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: bad_deployment on `geo` `{"version": "20260912.1"}` (cites `ev-9d7f47-001, ev-9d7f47-002`)
- **Proposal (llm):** `execute_rollback` on `geo` `{"to_version": "sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a"}`. Rationale (planner text, untrusted): The verified root cause is a bad deployment of the geo service, which is causing a crash loop. The evidence shows that the deployment version 20260912.1 introduced the issue, and the service is in a CrashLoopBackOff state. The recommended action for a bad deployment is to execute a rollback to a previous version. The rollback should be done to the last healthy revision, which can be determined by the get\_k8s\_like\_events and get\_recent\_deployments evidence.
- **Approval:** approved by `console-user` (answered at the console prompt: interactive terminal; typed the target service's name; answered after 121.9 s; login name kept in the local decision log), approval mode `manual`, apr-21dae2eb3e, 121.9 s after the request
- **Approval decision log** (append-only):
  - 2026-11-07T05:56:52.678139+00:00 requested by `recap`: run run-9d7f47104d; expires 2026-11-07T06:11:52.678139+00:00
  - 2026-11-07T05:58:54.573580+00:00 approved by `console-user`: interactive terminal; typed the target service's name; answered after 121.9 s; login name kept in the local decision log
  - 2026-11-07T05:58:58.321360+00:00 consumed by `recap`
- **Write:** `execute_rollback` on `geo` `{"to_version": "sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a"}`: ok True; geo generation 2 -> 3; geo pod template image: ghcr.io/sregym/hotel-reservation:sha-d2c036bc3d1138f5a0bdaffd3d87f37522c3461a@sha256:1c685e1c4c304f327952397b3a99c54dadb086607774d71f222b200ec0d2e65e; under approval `apr-21dae2eb3e`
- **Action result:** `execute_rollback` on `geo` → recovered
- **Oracle (`sregym-health-oracle`):** healthy before False → after True; verified True

## Usage

- 5 model requests for 5 agent decisions · 18,437 input (of which 7,480 came from the server's prompt cache) / 736 output tokens, as the server reported them
- 5 tool calls (4 reads, 1 approved write)
- Cost, as the run recorded it: $0.00 per request - self-hosted open-weight model (this machine or the owner's own VM), no paid API; 18,437 in / 736 out tokens reported by the server

## Scope and caveats

- One run of one scenario (`sregym-misconfig-app-hotel-res`) on a real cluster (`sregym`: real faults, reads and writes, not production): it shows that the path works end to end, not how often the model is right.
- Memory: not read (memory off for this run)
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-k8s-dependency-failures#fix-the-dependency-not-the-caller-1`, `guide-reading-signals#readiness-and-restarts-1`, `runbook-bad-deployment#remediation-1`, `runbook-k8s-crash-loops-and-rollouts#diagnosis-crash-loop-or-image-pull-1`, `runbook-memory-pressure#remediation-1`, `runbook-memory-pressure#symptoms-1`
- The verified run was saved to experience memory as `ep-9da7f7f0ef` (partition `live`).
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The proposal answer had to follow a JSON schema: an action from `execute_rollback`, `restart_service`, a listed service, and evidence IDs the run had seen.
- The recording keeps the model's answers as its server sent them, as evidence; it cannot be replayed: a replay is refused against a live target, and its prompts hold the real cluster's answers, which the simulator would not give.
- Wall time 930 s, of which 122 s waiting for the approval.

