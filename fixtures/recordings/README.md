# Recorded model runs (replay fixtures)

Each `.jsonl` file is a recording of a **real** language-model run: a header line, then
every HTTP exchange the provider made, in order (recap/providers/transport.py). The replay
provider (`RECAP_PROVIDER=replay`) answers from a recording instead of a model, so CI can
run an authentic model-driven incident offline, with no network and no model.

A replay is always labeled `REPLAY` (origin `replay`): its answers were written by the
model in the original run, not now. A request that is not in the recording stops the run
with `ReplayMismatch`; nothing is ever guessed.

| File | Model | Recorded | What happened |
|---|---|---|---|
| `ollama-qwen3-8b-bad-deployment.jsonl` | `qwen3:8b` (Q4_K_M, 8.2B, weights `sha256:a3de86cd1c13…`) via Ollama 0.34.4 on an M3 MacBook Air with 16 GB, `tools` mode, `think: false`, prompts-v4.7 | 2026-10-25 (recording `20261025-110603-ollama-b1ae62`) | `bad-deployment`, `use_memory: false`, BM25 retrieval, adaptive routing, simulation auto-approver (the approval channel does not change what the model sees). One investigator asked for 6 read-only tool calls in 5 steps (health; deployments; logs; events; the error-rate and latency metrics); it was offered `search_runbooks` and did not use it. It then claimed a `bad_deployment` of `checkout` `v2.4.1`, citing 5 evidence items (health, deployments, logs and both metrics), among them all 3 its rules relied on (V1-grounding: "cites 3 of the 3"). The verifier supported it; the model proposed `execute_rollback` to `v2.4.0`, the release before the bad one (VP4); the oracle verified recovery. 7 model requests, 19,946 input / 867 output tokens, about 3.5 minutes. Earlier fixtures: `20261025-100522-ollama-e200b7` (v4.6), `20261021-144429-ollama-a118d0` (v4.5), `20261021-141657-ollama-874b7e` (v4.4), `20261021-134004-ollama-5f0326` (v4.3), `20261021-091407-ollama-33e789` (v4.2). |

What a recording keeps: the request bodies (prompts are built from simulated incident
data only), the responses (Ollama's `/api/show` trimmed to capabilities, size, context
length and the weights file's name, never its local path; a hosted chat answer only the
fields the provider reads), and the rate-limit headers. No request or answer IDs are kept. It never holds an API key: keys are added only on the
wire, and every line is scrubbed for secrets and the user's home folder before it is
written.

To record a new one: run an LLM provider (`python scripts/demo.py --scenario
bad-deployment --provider ollama --no-memory`), then copy its recording from
`data/recordings/` here. Re-record whenever the prompts (now `prompts-v4.7`), the schemas
or the tool catalog change: the replay test fails loudly when the recording no longer
matches.
