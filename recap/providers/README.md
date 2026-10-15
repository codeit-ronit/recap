# Providers: the "brain" behind each agent

A provider decides what an agent does next. Every provider implements the `LLMProvider`
interface in `recap/core/interfaces.py`:

- `next_action(InvestigationRequest) -> InvestigationDecision`: call more read-only tools,
  or finish with root-cause hypotheses that cite evidence IDs.
- `propose_mitigation(ProposalRequest) -> ProposalDecision`: draft one remediation for the
  root cause the verifier accepted.

All providers return the same validated Pydantic types, so the harness never knows which one
is running. Swapping the brain does not change RAG, memory, routing, the safety gateway or
the harness.

| Name | Class | Kind | What it is |
|---|---|---|---|
| `deterministic` (default) | `DeterministicProvider` | scripted | Scripted baseline policy built from playbooks and heuristics. **Not an LLM.** No network, $0. |
| `anthropic` (opt-in) | `AnthropicProvider` | llm | Claude through the official Anthropic Python SDK. Uses **your own** API key and billing. |

## The honest default

`deterministic` is what the tests, CI and the demo use. It is a scripted policy, not a
language model: it follows fixed playbooks and parses simulator signals with fixed rules.
Its token numbers are *estimates* (about 4 characters per token) and its cost is always $0.
Results produced with it say nothing about how an LLM would perform.

Its one setting comes from `configs/budget.yaml`: `routing.familiar_similarity`, the memory
similarity at which a verified past fix may steer the investigator's first checks. It is the
same threshold the router uses, so changing it changes both.

## The optional Claude adapter (`anthropic`)

- **Opt-in only.** It is never used by the tests, CI or the default demo. Tests inject a
  fake client and never touch the network.
- **Your own key and billing.** Every decision is one paid Claude API call.
- **No silent fallback.** If you ask for `anthropic` but the SDK or the key is missing,
  RECAP stops with a `ConfigError`. It never quietly switches to another provider.

Enable it:

```bash
pip install -e '.[llm]'                 # installs the official anthropic SDK
export ANTHROPIC_API_KEY=...            # your own key (ANTHROPIC_AUTH_TOKEN also works)
export RECAP_PROVIDER=anthropic
python scripts/demo.py --scenario bad-deployment
```

The factory checks only these two environment variables before it builds the adapter. That
is deliberate: using a paid API should be an explicit choice, so a login profile on disk
alone is not enough.

### What one decision looks like

1. `prompts.py` renders the request as text. Trusted facts (incident ID, environment kind,
   tool list, earlier calls) are plain text. Everything else (alert text, tool output, runbook chunks, past
   incidents, model-written claims) is wrapped in `<untrusted_data id=... kind=... flags=...>`
   tags, HTML-escaped so it cannot close the tag early.
2. The adapter calls `client.beta.messages.parse(...)` with:
   - `model="claude-opus-5-5"`, `max_tokens=16000`, `output_config={"effort": "high"}`
     (all from `configs/providers.yaml`);
   - `output_format=` a closed Pydantic schema from `prompts.py`, so Claude must answer in
     JSON that the SDK validates;
   - server-side refusal fallback: `betas=["server-side-fallback-2026-07-01"]` and
     `fallbacks="default"`. If a safety classifier declines, the API retries on
     Anthropic's recommended fallback model; the adapter notes which model answered;
   - no `thinking` and no `temperature`: thinking is always on for this model and turning it
     off is rejected, so `effort` is the only depth control.
3. `stop_reason` is checked before the output is read. A refusal, a `max_tokens` cut-off or
   a reply that breaks the schema becomes a "finish" decision (investigation) or no draft
   (proposal), with a note saying why.
4. The reply is checked like any untrusted input: tool calls must use a listed tool and
   valid JSON arguments (`args_json`), cited evidence IDs must exist, and a remediation must
   be one of the allowed remediation tools and must not repeat an action that already
   failed. Every correction is recorded as a note in the trace.
5. Usage records the real token counts from the API. The cost is computed from
   `price_per_mtok` in `configs/providers.yaml`, so check current pricing before trusting it.
   A reply that breaks the schema is rejected inside the SDK before its token counts come
   back. That call was still billed, so its usage stays an estimate and the decision carries
   the note "provider usage not reported for this call".

API errors (rate limits, HTTP errors, network failures) become a `RecapError` and the
controller ends the run as `FAILED`. RECAP adds no retries on top of the SDK's own.

Even a perfect-looking answer only *proposes*. The safety gateway decides every tool call,
and a remediation still needs an exact approval.

## Prompts and versioning

`prompts.py` holds everything the model sees and must return: the system prompt, the two
renderers and the output schemas. `PROMPT_VERSION` (`prompts-v3`) is stored on every run,
so a trace always says which prompts produced it. Change anything in that file and bump the
version.

The system prompt is the same for every request. The environment kind comes from the
incident (`The target environment kind is '...'`), and only a `simulated` target is described
as a simulation rather than a real cluster.

## Red-team wrapper

`create_provider(..., planner="prompt_injected")` wraps any provider in
`recap.safety.redteam.PromptInjectedProvider`, a deliberately compromised planner that obeys
instructions hidden in untrusted data. It exists only to show that the safety gateway still
blocks a destructive action when the planner has been fooled.

## Files

| File | Purpose |
|---|---|
| `factory.py` | `create_provider(name, settings, planner=..., client=...)` and `describe_providers(settings)` |
| `deterministic.py`, `playbooks.py`, `heuristics.py` | the scripted default policy |
| `anthropic_provider.py` | the optional Claude adapter |
| `prompts.py` | versioned prompts and output schemas for LLM providers |

## Limitations (V1)

- No evaluation results with Claude have been measured in V1. All numbers RECAP reports
  come from the deterministic provider in the simulated environment.
- Wrapping untrusted data in tags reduces prompt-injection risk but does not remove it. The
  real protection is architectural: the gateway denies dangerous actions whatever the model
  says.
- The adapter targets the first-party Claude API. Server-side fallback is not available on
  Amazon Bedrock, Google Vertex AI or Microsoft Foundry.
