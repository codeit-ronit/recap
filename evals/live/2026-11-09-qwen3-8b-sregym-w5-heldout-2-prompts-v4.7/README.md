# Run run-db560f6568: sregym-w5-2

- **Final state:** FAILED: evidence stayed insufficient after the allowed investigation rounds; a human should take over
- **Agent decisions:** `ollama-vm:qwen3:8b`, origin **local_llm** (10 investigation requests in tools mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was no approval decision.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `042e0db212d1` · prompts `prompts-v4.7`
- **Environment:** sregym (simulated: False)
- **Time:** 2026-11-09T10:56:24.411971+00:00 to 2026-11-09T11:30:54.384139+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 12 HTTP exchanges with the model server (10 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads), 1 request(s) that got no answer; run.json and trace.jsonl name the original, `data/recordings/20261109-105613-ollama-vm-636299.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 2944 | 67 | 203.9 | yes |
| 2 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 3843 | 28 | 103.0 | yes |
| 3 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 3961 | 21 | 105.1 | yes |
| 4 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 4088 | 36 | 114.2 | yes |
| 5 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 4298 | 20 | 124.0 | yes |
| 6 | investigator | investigate | tools | `qwen3:8b` | tool\_request | 4973 | 74 | 173.7 | yes |
| 7 | investigator | investigate | tools | `qwen3:8b` | hypothesis | 5554 | 261 | 261.7 | yes |
| 8 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 5089 | 23 | 520.0 | yes |
| 9 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 5198 | 22 | 202.6 | yes |
| 10 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 5307 | 207 | 251.6 | yes |

- request 1: Ollama first loaded the model into memory (43.7s)
- request 5: ignored arguments list\_services does not take: service

## Read-only tool calls

Chosen by the model, run by the gateway:

- `investigator` → `get_k8s_like_events(service=rate, limit=10)`
- `investigator` → `get_service_logs(service=rate, limit=50, since_minutes=60)`
- `investigator` → `get_recent_deployments(service=rate, limit=5)`
- `investigator` → `get_service_health(service=rate)`
- `investigator` → `get_service_metrics(service=rate, metric=restarts, window_minutes=60)`
- `investigator` → `list_services()`
- `investigator` → `get_service_metrics(service=mongodb-rate, metric=restarts, window_minutes=60)`
- `investigator` → `get_service_logs(service=mongodb-rate, limit=50, since_minutes=60)`
- `infrastructure_investigator` → `get_recent_deployments(service=mongodb-rate)`
- `infrastructure_investigator` → `get_recent_deployments(service=rate)`

Policy decisions (every call is judged before it is sent):

- 9 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × ALLOW, decided by `R8-read-allow` (7 rules checked)

## Diagnosis, proposal, approval, action, oracle


## Usage

- 10 model requests for 11 agent decisions · 45,255 input (of which 17,556 came from the server's prompt cache) / 759 output tokens, as the server reported them
- 10 tool calls (10 reads)
- Cost, as the run recorded it: $0.00 per request - self-hosted open-weight model (this machine or the owner's own VM), no paid API; 45,255 in / 759 out tokens reported by the server

## Scope and caveats

- One run of one scenario (`sregym-w5-2`) on a real cluster (`sregym`: real faults, reads and writes, not production): it shows that the path works end to end, not how often the model is right.
- Memory: read
- Runbook retrieval (RAG) was on; chunks retrieved: `runbook-k8s-crash-loops-and-rollouts#diagnosis-crash-loop-or-image-pull-1`, `runbook-k8s-crash-loops-and-rollouts#verification-1`, `runbook-memory-pressure#overview-1`, `runbook-memory-pressure#symptoms-1`
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The recording keeps the model's answers as its server sent them, as evidence; it cannot be replayed: a replay is refused against a live target, and its prompts hold the real cluster's answers, which the simulator would not give.

