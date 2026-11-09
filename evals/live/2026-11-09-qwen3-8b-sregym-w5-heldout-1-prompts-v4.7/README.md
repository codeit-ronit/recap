# Run run-7bc5159b6d: sregym-w5-1

- **Final state:** FAILED: evidence stayed insufficient after the allowed investigation rounds; a human should take over
- **Agent decisions:** `ollama-vm:qwen3:8b`, origin **local_llm** (13 investigation requests in tools mode). Routing, verification, the safety policy and the oracle are RECAP's own code; the fix was no approval decision.
- **Model server:** Ollama 0.34.4 · `qwen3:8b` (`8.2B`, `Q4_K_M`) · weights `sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f`
- **Code:** `042e0db212d1` · prompts `prompts-v4.7`
- **Environment:** sregym (simulated: False)
- **Time:** 2026-11-09T08:37:19.416426+00:00 to 2026-11-09T09:29:35.664500+00:00
- **Recording:** `recording.jsonl` in this folder: a header and 15 HTTP exchanges with the model server (13 model requests with the server's responses; metadata and answers are trimmed to the fields RECAP reads), 1 recorded stop(s) (budget or clock); run.json and trace.jsonl name the original, `data/recordings/20261109-083708-ollama-vm-363f31.jsonl`, which is not published.

## Model requests

Token counts are the server's own; model text is untrusted.

| # | agent | purpose | mode | model (as reported) | answer | in | out | seconds | ok |
|---|---|---|---|---|---|---|---|---|---|
| 1 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2860 | 62 | 337.3 | yes |
| 2 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2637 | 70 | 339.6 | yes |
| 3 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 4064 | 42 | 201.5 | yes |
| 4 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 2978 | 70 | 337.2 | yes |
| 5 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 4330 | 56 | 237.9 | yes |
| 6 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3404 | 70 | 255.0 | yes |
| 7 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 4578 | 50 | 257.3 | yes |
| 8 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 3672 | 71 | 310.0 | yes |
| 9 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 5259 | 63 | 329.2 | yes |
| 10 | infrastructure\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 6616 | 46 | 294.8 | yes |
| 11 | telemetry\_investigator | investigate | tools | `qwen3:8b` | tool\_request | 4101 | 71 | 419.2 | yes |
| 12 | telemetry\_investigator | investigate | tools | `qwen3:8b` | hypothesis | 4373 | 251 | 601.6 | yes |
| 13 | investigator | investigate | tools | `qwen3:8b` | hypothesis | 8381 | 396 | 615.5 | yes |

- request 1: Ollama first loaded the model into memory (43.9s)
- request 2: Ollama first loaded the model into memory (44.0s)
- request 7: ignored arguments list\_services does not take: dependency, service
- request 7: ignored arguments list\_services does not take: dependency, service
- request 7: dropped a repeat of mcp:list\_services()
- request 12: dropped unknown IDs \['runbook-memory-pressure#symptoms-1', 'runbook-k8s-crash-loops-and-rollouts#diagnosis-crash-loop-or'\]

## Read-only tool calls

Chosen by the model, run by the gateway:

- `infrastructure_investigator` → `get_k8s_like_events(service=frontend, limit=10)`
- `infrastructure_investigator` → `get_k8s_like_events(service=geo, limit=10)`
- `telemetry_investigator` → `get_service_metrics(service=frontend, metric=restarts, window_minutes=5)`
- `telemetry_investigator` → `get_service_logs(service=frontend, limit=50, since_minutes=5)`
- `infrastructure_investigator` → `get_service_health(service=frontend)`
- `infrastructure_investigator` → `get_service_health(service=geo)`
- `telemetry_investigator` → `get_service_metrics(service=geo, metric=restarts, window_minutes=5)`
- `telemetry_investigator` → `get_service_metrics(service=profile, metric=restarts, window_minutes=5)`
- `infrastructure_investigator` → `get_recent_deployments(service=frontend, limit=5)`
- `infrastructure_investigator` → `get_recent_deployments(service=geo, limit=5)`
- `telemetry_investigator` → `get_service_logs(service=geo, limit=50, since_minutes=5)`
- `telemetry_investigator` → `get_service_logs(service=profile, limit=50, since_minutes=5)`
- `infrastructure_investigator` → `list_services()`
- `telemetry_investigator` → `get_service_metrics(service=recommendation, metric=restarts, window_minutes=5)`
- `telemetry_investigator` → `get_service_metrics(service=reservation, metric=restarts, window_minutes=5)`
- `infrastructure_investigator` → `get_k8s_like_events(service=profile, limit=10)`
- `infrastructure_investigator` → `get_k8s_like_events(service=recommendation, limit=10)`
- `infrastructure_investigator` → `get_recent_deployments(service=search)`
- `infrastructure_investigator` → `get_k8s_like_events(service=user)`
- `telemetry_investigator` → `get_service_logs(service=recommendation, limit=50, since_minutes=5)`
- `telemetry_investigator` → `get_service_logs(service=reservation, limit=50, since_minutes=5)`

Policy decisions (every call is judged before it is sent):

- 20 × ALLOW, decided by `R8-read-allow` (8 rules checked)
- 1 × ALLOW, decided by `R8-read-allow` (7 rules checked)

## Diagnosis, proposal, approval, action, oracle


## Usage

- 13 model requests for 14 agent decisions · 57,253 input (of which 20,323 came from the server's prompt cache) / 1,318 output tokens, as the server reported them
- 21 tool calls (21 reads)
- Cost, as the run recorded it: $0.00 per request - self-hosted open-weight model (this machine or the owner's own VM), no paid API; 57,253 in / 1,318 out tokens reported by the server

## Scope and caveats

- One run of one scenario (`sregym-w5-1`) on a real cluster (`sregym`: real faults, reads and writes, not production): it shows that the path works end to end, not how often the model is right.
- Memory: read
- Runbook retrieval (RAG) was on; chunks retrieved: `guide-k8s-dependency-failures#fix-the-dependency-not-the-caller-1`, `guide-reading-signals#readiness-and-restarts-1`, `runbook-k8s-crash-loops-and-rollouts#diagnosis-crash-loop-or-image-pull-1`, `runbook-memory-pressure#symptoms-1`
- Sampling: temperature 0.0, seed 7, thinking off, at most 1024 output tokens per answer, context window 16384 tokens.
- The recording keeps the model's answers as its server sent them, as evidence; it cannot be replayed: a replay is refused against a live target, and its prompts hold the real cluster's answers, which the simulator would not give.

