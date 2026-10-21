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
| `ollama-qwen3-8b-bad-deployment.jsonl` | `qwen3:8b` (Q4_K_M, 8.2B, weights `sha256:a3de86cd1c13…`) via Ollama 0.34.4 on an M3 MacBook Air with 16 GB, `tools` mode, `think: false`, prompts-v4.2 | 2026-10-21 (recording `20261021-091407-ollama-33e789`) | `bad-deployment`, `use_memory: false`, adaptive routing, simulation auto-approver (the approval channel does not change what the model sees). One investigator asked for 5 read-only tool calls in 3 steps (health and the error-rate metric; deployments and events; logs), then cited a `bad_deployment` of `checkout` `v2.4.1` with 4 evidence IDs; the verifier supported it; the model proposed `execute_rollback` to `v2.4.0`; the oracle verified recovery. 5 model requests, 12,891 input / 744 output tokens, about 2 minutes. Recorded after prompts-v4.2 removed example values that matched this scenario's answer (`v2.4.1`, `v2.4.0`): the model still took both from the deployment evidence. |

What a recording keeps: the request bodies (prompts are built from simulated incident
data only), the responses (Ollama's `/api/show` trimmed to capabilities, size, context
length and the weights file's name, never its local path), and the rate-limit headers (no
request IDs). It never holds an API key: keys are added only on the
wire, and every line is scrubbed for secrets and the user's home folder before it is
written.

To record a new one: run an LLM provider (`python scripts/demo.py --scenario
bad-deployment --provider ollama --no-memory`), then copy its recording from
`data/recordings/` here. Re-record whenever the prompts (now `prompts-v4.2`), the schemas
or the tool catalog change: the replay test fails loudly when the recording no longer
matches.
