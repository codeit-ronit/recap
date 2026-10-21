# Providers: the "brain" behind each agent

A provider decides what an agent does next. Every provider implements the `LLMProvider`
interface in `recap/core/interfaces.py`:

- `next_action(InvestigationRequest) -> InvestigationDecision`: call more read-only tools,
  or finish with root-cause hypotheses that cite evidence IDs.
- `propose_mitigation(ProposalRequest) -> ProposalDecision`: draft one remediation for the
  root cause the verifier accepted.

All providers return the same validated Pydantic types, so the harness never knows which one
is running. Swapping the brain does not change RAG, memory, routing, the safety gateway or
the harness. Which one runs is server configuration: `configs/providers.yaml`, or
`RECAP_PROVIDER=<name>` (the CLI also takes `--provider <name>`).

| Name | Class | Origin (run badge) | What it is | Cost |
|---|---|---|---|---|
| `deterministic` (default) | `DeterministicProvider` | `scripted` | Scripted baseline policy built from playbooks and heuristics. **Not an LLM.** | $0, offline |
| `ollama` | `OllamaProvider` | `local_llm` | A real open-weight model on this machine (`qwen3:8b`) through Ollama's `/api/chat`. | $0, offline |
| `groq` | `FreeHostedProvider` | `free_hosted_llm` | A real hosted model (`openai/gpt-oss-120b`) on Groq's **Free plan**, behind a fail-closed guard. | $0 (free plan, checked every call) |
| `replay` | the recorded provider + `ReplayTransport` | `replay` | Answers from a recording of an earlier real-model run. No model is called. | $0, offline |
| `anthropic` | `AnthropicProvider` | `paid_llm` | Claude through the official SDK. **Paid; disabled** for this $0 project. | billed per call |

The dashboard shows the origin as a badge on every run (SCRIPTED, LOCAL LLM, FREE HOSTED
LLM, REPLAY), so a scripted or replayed run can never pass for live model reasoning.

## The honest default

`deterministic` is what the tests, CI and the default demo use. It follows fixed playbooks
and parses simulator signals with fixed rules. Its token numbers are *estimates* (about 4
characters per token) and its cost is always $0. Results produced with it say nothing about
how an LLM would perform.

## Real models (M2): how one decision works

`OllamaProvider` and `FreeHostedProvider` share one loop (`http_llm.py`) and one versioned
step protocol (`protocol.py`, **prompts-v4**). For each decision:

1. **Render** the request: a static system prompt (the rules, and RECAP's failure
   mechanisms in generic SRE terms), then one user message with the role, the incident, the
   tools, the agent's previous calls, what any agent already observed, and the context pack.
   Everything RECAP did not write is inside `<untrusted_data>` tags. "Step N of M" comes
   last, so a local server can reuse its cache for the shared start.
2. **Constrain** the answer with a schema built for this request from RECAP's trusted
   catalog: tool names, services, metrics and mechanisms are enums, and evidence IDs are an
   enum of the IDs this agent can see. In `tools` mode the read tools are native functions
   plus `submit_findings`. On the last step only finishing is offered.
3. **Send** one HTTP request through the transport (`transport.py`): no SDK, no hidden
   retries. Every exchange is recorded under `data/recordings/` without the API key.
4. **Parse** the answer (`tool_request`, `hypothesis`, `need_more_evidence`, `stop`; for a
   proposal `final_proposal` or `stop`) and check it again: unlisted tools, foreign
   arguments, repeated calls, unknown IDs and services outside the allowlist are dropped with
   a note in the trace.
5. **Unusable?** Send the reason back once (a corrective retry). Still unusable: the agent
   finishes without a decision. There is **never** a fallback to scripted decisions.

Each request becomes a `ModelCall` in the decision (an `llm.call` trace event): model asked
for and model that answered, mode, attempt, HTTP status, exact token counts as the server
reported them, duration, finish reason, a prompt fingerprint, and excerpts of the answer
and of any reasoning.

### `ollama`: local, offline

- Loopback only (`http://127.0.0.1:11434`); `...-cloud` models are refused; RECAP never
  downloads a model (`ollama pull` is the owner's decision).
- Before the first request, with no inference: `/api/version` and `/api/show` (capabilities
  must include `tools` for tools mode, and `thinking` if `think: true`; `context_tokens` must
  fit the model's window).
- A prompt that may not fit `num_ctx` is refused before sending: Ollama would silently drop
  the start of the prompt, where the safety rules are.
- Investigation steps use native tool calls (`mode: tools`): measured on 2026-10-21, a tool
  step took about 16 s and 57 output tokens, against about 75 s and 740 tokens in JSON mode.
  Proposals always use a JSON schema.

```bash
OLLAMA_NO_CLOUD=1 ollama serve            # in another terminal
python scripts/demo.py --scenario bad-deployment --provider ollama
```

### `groq`: the free hosted plan, fail-closed

Nothing is sent, not even the model-list check, unless every one of these holds
(`quota.py`, ADR 0017):

1. `RECAP_ALLOW_LIVE_FREE_INFERENCE=1` for this process (never in CI);
2. the model is in `free_tier.allowed_models`;
3. `GROQ_API_KEY` is set in the environment (keys are never read from files);
4. `RECAP_GROQ_FREE_PLAN_CONFIRMED_ON=YYYY-MM-DD`: the date the owner checked the Groq
   console's billing page and saw the Free plan, at most 7 days ago;
5. the provider was not stopped by the tripwire;
6. RECAP's own caps have room (below the free plan's 30 RPM / 1K RPD / 8K TPM / 200K TPD).

After every response, `x-ratelimit-limit-requests` must equal the free plan's daily limit
(1,000); anything else, a 402 or `blocked_api_access` stops the provider until
`python scripts/providers.py clear-stop groq`. A 429 is retried once if `retry-after` is
short; otherwise the run ends `QUOTA_EXHAUSTED` (deferred). It never switches provider.

```bash
export GROQ_API_KEY=...                                    # your key, in your shell only
export RECAP_GROQ_FREE_PLAN_CONFIRMED_ON=2026-10-21        # after checking the billing page
RECAP_ALLOW_LIVE_FREE_INFERENCE=1 python scripts/demo.py --scenario bad-deployment --provider groq
```

### `replay`: a real run, offline

`RECAP_PROVIDER=replay` (recording from `configs/providers.yaml` or
`RECAP_REPLAY_RECORDING`) rebuilds the recorded provider and answers each request from the
recording, matched by a fingerprint that ignores run-specific random IDs. A request that is
not in the recording stops the run with `ReplayMismatch`. CI replays
`fixtures/recordings/ollama-qwen3-8b-bad-deployment.jsonl` (ADR 0018).

## The optional Claude adapter (`anthropic`): paid, disabled

It calls the paid Claude API, so it needs `RECAP_ALLOW_PAID_LLM=1` on top of the SDK
(`pip install -e '.[llm]'`) and `ANTHROPIC_API_KEY`. This project's budget is $0: leave it
off. It still uses prompts-v3 (`prompts.py`) with static schemas. Tests use a fake client.

## Inspecting providers

```bash
python scripts/providers.py              # readiness of every provider (no inference)
python scripts/providers.py usage        # live requests and tokens per provider
curl -s localhost:8000/providers         # the same, from the API (keys: present or missing)
```

## Files

| File | Purpose |
|---|---|
| `factory.py` | `create_provider(name, settings, ...)`: builds providers from `configs/providers.yaml`; recordings; the paid opt-in |
| `protocol.py` | prompts-v4: the step protocol's text, per-request schemas and the parser |
| `http_llm.py` | the shared decision loop: render, send, parse, one corrective retry, `ModelCall`s |
| `ollama_provider.py` | the local provider |
| `openai_compatible.py` | the free hosted provider (Groq) |
| `quota.py` | the usage ledger and the free-tier guard |
| `transport.py` | live HTTP, recording, replay and fingerprints |
| `catalog.py` | provider readiness for `GET /providers` (no inference, no hosted calls) |
| `deterministic.py`, `playbooks.py`, `heuristics.py` | the scripted default policy |
| `anthropic_provider.py`, `prompts.py` | the paid adapter and prompts-v3 (section builders shared with v4) |

## Limitations

- Real runs are slow locally (about 15-50 s per decision on a 16 GB M3) and rate-limited on
  the free plan (about two large requests a minute).
- Runs are not reproducible at the model level (each prompt contains a random incident ID);
  only a replay is exact.
- A schema constrains an answer's form, not its truth: the verifier, the policy, the
  approval and the oracle stay in charge.
- Real models have not been red-teamed yet (planned for M3/M4).
