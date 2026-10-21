# ADR 0016: Real model inference only from local or verified-free providers, through one constrained step protocol

**In one sentence:** RECAP's real "brains" are a local Ollama model and a hosted free plan
(Groq Free), both spoken to over plain HTTP through one versioned step protocol
(prompts-v4) whose JSON schemas are built per request from RECAP's trusted catalog. The
model can then only name listed tools, allowlisted services and evidence IDs it can see;
an unusable answer gets one corrective retry and never a scripted stand-in.

## Status

Accepted (M2, 2026-10-21). Builds on [ADR 0003](0003-deterministic-default-provider.md)
(the scripted policy stays the default) and on the label-free views of
[ADR 0010](0010-label-free-views-and-a-yes-no-oracle.md). The guard that keeps the hosted
provider at $0 is [ADR 0017](0017-fail-closed-free-tier-guard-and-a-deferred-state.md);
recordings and replay are [ADR 0018](0018-record-every-model-exchange-and-replay-in-ci.md).

## Context

- V1's only real-model adapter was the paid Claude API, and the owner's budget for this
  project is $0 (no paid API, no card, no paid fallback).
- The audit and the master prompt require *authentic* model reasoning: the model chooses
  the next observation, cites evidence, may stop or ask for more evidence, and proposes a
  structured fix, while never seeing the oracle or any evaluation label.
- Free and local models are small or rate-limited. Measured on 2026-10-21 with real RECAP
  prompts, `qwen3:8b` on a 16 GB M3 reads about 160 tokens/s and writes about 14 tokens/s,
  and with V1's prompts (prompts-v3) it labeled a bad release as a "dependency failure"
  and put the version under a key the verifier does not read.

## Decision

- **Two real providers behind the unchanged `LLMProvider` interface**, sharing one loop in
  [recap/providers/http_llm.py](../../recap/providers/http_llm.py):
  - [OllamaProvider](../../recap/providers/ollama_provider.py): loopback only, no cloud
    models, never downloads; checks `/api/show` capabilities and the context window before
    the first request; refuses a prompt that may not fit `num_ctx` (Ollama would silently
    drop the prompt's start, where the safety rules are); exact token counts from
    `prompt_eval_count` / `eval_count`.
  - [FreeHostedProvider](../../recap/providers/openai_compatible.py): an OpenAI-compatible
    chat completion for Groq's Free plan, with strict JSON schema; it sends only an
    allowlisted set of fields (never `service_tier`, a `models` fallback list, built-in
    tools, `stream`, `n` or `logprobs`) and checks that the answering model is the
    requested one.
- **Plain HTTP with httpx, no vendor SDKs.** One request is one HTTP call: no hidden retries
  that spend a free quota, and every status code is handled where it can be read.
- **The step protocol** ([recap/providers/protocol.py](../../recap/providers/protocol.py),
  `prompts-v4`):
  - A step answers `tool_request`, `hypothesis`, `need_more_evidence` or `stop`; a proposal
    answers `final_proposal` or `stop`. `need_more_evidence` and `stop` finish without a
    diagnosis, so the verifier finds the evidence insufficient and the controller escalates
    or hands over to a human.
  - Each request's JSON schema is built from RECAP's trusted registry: tool names,
    services, metrics and mechanisms are enums, and evidence IDs are an enum of the IDs the
    agent can see. Every object is closed and fully required, which is what strict
    structured outputs accept, and lists have `maxItems` (Ollama enforces it while
    generating; a small model once repeated two citation IDs until its output limit).
  - The system prompt defines RECAP's failure mechanisms in generic SRE terms (no service
    and no scenario), and hypotheses carry typed `version` / `dependency` fields.
  - Stable sections come first and "step N of M" last, so a local server can reuse its
    cache for the shared start of every step.
- **Two answer modes.** `tools` (native tool calls plus a `submit_findings` function) and
  `json` (the per-request schema). Proposals always use `json`. Ollama runs investigation
  steps in `tools` mode: a tool step measured 57 output tokens and 16 s, against about 740
  tokens and 75 s in `json` mode. Groq uses `json` (it cannot combine strict schemas with
  tools).
- **Model output is untrusted and checked again** after the schema: unlisted tools, a
  tool's foreign arguments, repeats of a call already observed, unknown IDs and services
  outside the allowlist are dropped with a note that the trace shows; at most 2 calls run
  per step; a remediation must be in the catalog, on the diagnosed service, cite real
  evidence and not repeat a failed fix. The safety gateway still decides every call.
- **One corrective retry, then nothing.** An unusable answer is sent back once with the
  reason (`max_attempts: 2`). If it is still unusable, the agent finishes without a decision
  (or the proposal is empty). There is never a scripted fallback.
- **Every request is a `ModelCall`** (provider, model asked for and model that answered,
  origin, mode, attempt, status, exact tokens, duration, finish reason, prompt fingerprint,
  answer and reasoning excerpts, rate-limit headers), emitted as an `llm.call` trace event.
  `Usage.model_requests` counts real requests; `ProviderInfo.origin` (`scripted`,
  `local_llm`, `free_hosted_llm`, `replay`, `paid_llm`) is the dashboard's run-origin badge.
- **The paid Claude adapter stays, disabled:** it now needs `RECAP_ALLOW_PAID_LLM=1` on top
  of a key, so an API key alone never switches on a paid API.

## Consequences

### Good

- A real `qwen3:8b` investigation resolved `bad-deployment` end to end (2026-10-21, 10 model
  requests, 26,911 input / 1,580 output tokens, $0); a real `gpt-oss-120b` run on Groq
  Free passed the free-plan check on every response. The verifier, not the model, decided
  what counts: an under-evidenced claim was "insufficient" and the router escalated.
- The model cannot invent a tool, a service or an evidence ID through the schema, and
  cannot exceed 2 calls per step or repeat an observation through the parser.
- Swapping models is configuration (`configs/providers.yaml`); the harness, verifier,
  gateway and dashboard do not change.

### Bad

- Real runs are slow locally (about 1 minute per decision with two agents sharing one
  model) and rate-limited on the free plan (about two large requests a minute).
- Runs are not reproducible at the model level: the incident ID in the prompt differs per
  run, so even at temperature 0 the model's path varies. Only a replay is exact (ADR 0018).
- A schema constrains the form of an answer, not its truth: a valid answer can still be
  wrong, which is why the verifier and the gateway stay in charge.
- prompts-v3 (Claude adapter) and prompts-v4 (free/local providers) now coexist; both are
  versioned and recorded on every run.

## Alternatives considered

- **Vendor SDKs (openai, ollama, anthropic).** Their automatic retries spend free quota
  silently and hide status codes; plain HTTP keeps every request visible.
- **A growing chat transcript per agent with `role: tool` messages.** Re-rendering a bounded
  context pack each step keeps the newest critical evidence within budget and keeps
  providers stateless, which also makes replay possible.
- **Free-form `args_json` strings (prompts-v3).** Per-request enums let the server enforce
  validity while generating, which matters most for small models.
- **Falling back to the scripted policy when a model fails.** That would label scripted
  decisions as model reasoning; the run fails or escalates instead.

## Amended after the M2 review

Two independent reviews (security, correctness) of the M2 code changed these points; each
fix has a test that fails when the fix is reverted (`tests/unit/test_m2_review.py`).

- **Every HTTP request is a ModelCall, including failed ones.** A subclass now appends every
  exchange to a log before it retries or raises: a retried 429 or 5xx is its own
  `llm.call`, and when a decision fails after requests went out (a refusal, a quota stop, the
  tripwire), they are attached to the exception (`attach_model_calls`) and the harness and
  controller record them before the error goes on. Before, the request most likely to have
  cost something was exactly the one missing from the trace and from `model_requests`.
- **The origin is fixed by the code, not the configuration:** `OllamaProvider` is
  `local_llm` and `FreeHostedProvider` is `free_hosted_llm`; the config validator refuses any
  other pairing, and a hosted `base_url` must use `https://`.
- **prompts-v4.1:** tools-mode definitions no longer list the step's evidence IDs (servers do
  not enforce tool-argument schemas, the IDs are in the prompt, and identical definitions let
  a local server cache the start of the prompt); feedback sent back to the model never quotes
  the model's own text; model text loses control characters (terminal escape codes); an
  oversized number is an unusable answer, not a crash; the function the model called decides
  the tool, never a `tool_name` key inside its arguments; a shared argument (`limit`) no
  longer claims one tool's range; a catalog whose argument names collide with the protocol's
  fields is refused at startup.
- **Ollama:** model names ending in "cloud" are refused in any case, and so is a local name
  that `/api/show` reports as served from a remote host.
- The committed replay fixture was re-recorded with prompts-v4.1 (8 requests, 21,489 / 771
  tokens, RESOLVED in one round).

## Amended after the human-approved run review

- **prompts-v4.2:** the schemas' example values (`"v2.4.1"`, `"v2.4.0"`) were exactly the
  `bad-deployment` scenario's answer, so a correct diagnosis could not be told apart from a
  copied example. Fixed text now carries no example value, and
  `tests/unit/test_prompt_neutrality.py` checks every scenario's facts against all of it
  ([ADR 0019](0019-checkable-evidence-of-a-real-model-run.md)). The paid adapter's prompts
  became prompts-v3.1 for the same reason.
- `ProviderInfo.server` now records what the Ollama server reported (version, capabilities,
  size, quantization, weights digest), and the run record names the code version.
