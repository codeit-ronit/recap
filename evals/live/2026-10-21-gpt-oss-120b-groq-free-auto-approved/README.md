# Run run-cf423a95ea: bad-deployment

- **Final state:** RESOLVED: oracle verified recovery (simulated environment)
- **Agent decisions:** `groq:openai/gpt-oss-120b`, origin **free_hosted_llm** (7 investigation requests in JSON-schema mode; 1 proposal request in JSON-schema mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was approved by `auto-approver[simulation]`.
- **Free-plan check:** 8 recorded responses carried `x-ratelimit-limit-requests` = `1000` (requests per day; RECAP stops the provider if it is not the free plan's); remaining requests went from 997 to 990
- **Code:** `not recorded` · prompts `prompts-v4`
- **Environment:** simulated (simulated: True)
- **Time:** 2026-10-21T06:04:12.851420+00:00 to 2026-10-21T06:08:18.527983+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 9 HTTP exchanges with the model server (8 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads); run.json and trace.jsonl name the original, `data/recordings/20261021-060412-groq-1cbdb8.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | investigator | investigate | json | `openai/gpt-oss-120b` | tool\_request | 2263 | 209 | 0.9 | yes |
| 2 | investigator | investigate | json | `openai/gpt-oss-120b` | invalid | – | – | 1.3 | no |
| 3 | investigator | investigate | json | `openai/gpt-oss-120b` | invalid | – | – | 1.2 | no |
| 4 | infrastructure\_investigator | investigate | json | `openai/gpt-oss-120b` | tool\_request | 2293 | 254 | 1.2 | yes |
| 5 | telemetry\_investigator | investigate | json | `openai/gpt-oss-120b` | tool\_request | 2354 | 264 | 1.5 | yes |
| 6 | telemetry\_investigator | investigate | json | `openai/gpt-oss-120b` | hypothesis | 2986 | 321 | 2.2 | yes |
| 7 | infrastructure\_investigator | investigate | json | `openai/gpt-oss-120b` | hypothesis | 2667 | 254 | 1.2 | yes |
| 8 | controller | propose | json | `openai/gpt-oss-120b` | final\_proposal | 2802 | 383 | 1.6 | yes |

- request 2: the server rejected an answer that broke the schema
- request 2: unusable: the answer is not one JSON object (the answer is empty).
- request 3: the server rejected an answer that broke the schema
- request 3: unusable: the answer is not one JSON object (the answer is empty).

## Read-only tool calls

Chosen by the model, run by the gateway:

- `investigator` → `list_services()`
- `investigator` → `get_recent_deployments(service=checkout, limit=5)`
- `infrastructure_investigator` → `get_service_health(service=checkout)`
- `telemetry_investigator` → `get_service_health(service=checkout)`
- `infrastructure_investigator` → `get_k8s_like_events(service=checkout, limit=20)`
- `telemetry_investigator` → `get_service_logs(service=checkout, limit=50, since_minutes=60)`

Policy decisions (every call is judged before it is sent):

- 1 × ALLOW, decided by `R8-read-allow` (7 rules checked)
- 5 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × APPROVAL_REQUIRED, decided by `R11-reversible-approval` (12 rules checked)
- 1 × ALLOW, decided by `R12-approval-binding` (13 rules checked)

## Diagnosis, proposal, approval, action, oracle

- **Verifier:** supported: bad_deployment on `checkout` `{"version": "v2.4.1"}` (cites `ev-cf423a-002, ev-cf423a-004, ev-cf423a-006, ev-cf423a-003`)
- **Proposal (llm):** `execute_rollback` on `checkout` `{"to_version": "v2.4.0"}`. Rationale (planner text, untrusted): The verified root cause is a bad\_deployment of checkout version v2.4.1, evidenced by recent deployment, elevated error rate, high latency, and NullPointerExceptions in the new version's logs. Rolling back to the last known good version (v2.4.0) is the prescribed remediation for a bad deployment.
- **Approval:** approved by `auto-approver[simulation]` (the simulation auto-approver, not a person), approval mode `auto`, apr-4d2b57be5c, 0.0 s after the request
- **Approval decision log** (append-only):
  - 2026-10-21T06:08:17.917299+00:00 requested by `recap`: run run-cf423a95ea; expires 2026-10-21T06:23:17.917299+00:00
  - 2026-10-21T06:08:17.919122+00:00 approved by `auto-approver[simulation]`
  - 2026-10-21T06:08:17.925462+00:00 consumed by `recap`
- **Write:** `execute_rollback` on `checkout` `{"to_version": "v2.4.0"}`: ok True; checkout: version v2.4.1 -> v2.4.0; checkout: pods replaced; under approval `apr-4d2b57be5c`
- **Action result:** `execute_rollback` on `checkout` → recovered
- **Oracle (`simulated-health-oracle`):** healthy before False → after True; verified True; `checkout` error rate 0.32 → 0.0

## Usage

- 8 model requests for 7 agent decisions · 15,365 input / 1,685 output tokens, as the server reported them
- 7 tool calls (6 reads, 1 approved write)
- Cost: $0.00 - free plan (owner-confirmed; every response's limit header matched the free plan); 15,365 in / 1,685 out tokens reported by the server, 2 request(s) unreported

## Scope and caveats

- One run of one simulated scenario (`bad-deployment`): it shows that the path works end to end, not how often the model is right.
- Memory: not read (memory off for this run)
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-reading-signals#error-rate-1`, `guide-reading-signals#latency-1`, `runbook-bad-deployment#remediation-1`, `runbook-bad-deployment#verification-1`, `runbook-memory-pressure#remediation-1`, `shop-architecture#service-level-objectives-1`
- The verified run was saved to experience memory as `ep-c7ed0063c0` (partition `demo`).
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window None tokens.
- The recording replays offline with the same RECAP code and prompts (`prompts-v4`); after a prompt change a replay stops with ReplayMismatch instead of guessing.
- Wall time 246 s, of which 0 s waiting for the approval.

