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
| `ollama-qwen3-8b-bad-deployment.jsonl` | `qwen3:8b` (Q4_K_M, 8.2B) via Ollama 0.34.4 on an M3 MacBook Air with 16 GB, `tools` mode, `think: false` | 2026-10-21 (recording `20261021-060412-ollama-2ac26e`) | `bad-deployment`, `use_memory: false`, adaptive routing, simulation auto-approver (the approval channel does not change what the model sees). One investigator chose 6 read-only tool calls over 5 steps (health, deployments, events, logs, two metrics), then cited a `bad_deployment` of `checkout` `v2.4.1`; the verifier supported it; the model proposed `execute_rollback` to `v2.4.0`; the oracle verified recovery. 7 model requests, 18,331 input / 809 output tokens, about 2.4 minutes. |

What a recording keeps: the request bodies (prompts are built from simulated incident
data only), the responses (Ollama's `/api/show` trimmed to capabilities, size and context
length), and the rate-limit headers. It never holds an API key: keys are added only on the
wire, and every line is scrubbed for secrets and the user's home folder before it is
written.

To record a new one: run an LLM provider (`python scripts/demo.py --scenario
bad-deployment --provider ollama --no-memory`), then copy its recording from
`data/recordings/` here. Re-record whenever `prompts-v4`, the schemas or the tool catalog
change: the replay test fails loudly when the recording no longer matches.
