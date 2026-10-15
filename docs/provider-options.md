# Free LLM provider options for RECAP

**Checked on 2026-10-15.** Free tiers, model lists and limits change without notice. Nothing
on this page promises that any model or tier stays free. RECAP must re-check free status at
setup and before every live run (section 8), and must refuse to call a provider when it
cannot.

**How this page was researched.** Only official web pages and documentation were read. No
request of any kind was sent to an LLM API (no keys, no probes, not even a model-list call),
no account was created, and nothing was downloaded with `ollama pull`. The local facts come
from read-only checks on this Mac: reading Ollama's files on disk and running
`ollama --version`, which printed only the client version because no Ollama server was
running.

**Labels used on this page**

- **Verified**: stated on the official page linked next to it, read on 2026-10-15.
- **Local check**: observed on this machine, with no network LLM call.
- **Inference**: our own reasoning from verified facts. Treat it as a hypothesis to test in
  M2, not as a fact.

**Vocabulary.** RPM and RPD mean requests per minute and per day; TPM and TPD mean tokens per
minute and per day. *Tool calling* means the model returns a structured request to call one
of our functions. *Structured output* means the reply is forced to match a JSON schema.
*Fail closed* means that when in doubt, RECAP does not send the request.

---

## 1. Recommendation

| Role | Choice | Why, in short | Biggest risk |
|---|---|---|---|
| Hosted primary | **Groq, Free plan, `openai/gpt-oss-120b`**. Spare model in the same account: `openai/gpt-oss-20b`. | A production model with tool calling and strict JSON-schema output. It has the largest *published* free quota of the options checked (1K requests per day). The Free plan has no payment method, and the owner can switch on an org-wide model allowlist. | 8K tokens per minute is tight for RECAP's prompts. Free status can be confirmed only in the Groq console, not through the API. |
| Hosted backup | **OpenRouter, one `:free` model chosen at setup by an API check** (no fixed model ID). | The only option where RECAP can check free status through the API before every call (key info plus zero prices). A request can also be told to fail when no zero-price endpoint exists. | Only 50 free requests per day unless credits are bought, and they must not be. The list of free models changes often, and some free providers may train on prompts. |
| Local | **Ollama on this Mac.** Now: `llama3.2:3b` (already installed; plumbing tests only). After owner approval: **`qwen3:8b` (5.2 GB download)** for real local runs. | $0, offline, no quota, and nothing leaves the machine. Both models are tagged `tools`. | Slow on 16 GB. Small models are unreliable at multi-step diagnosis. The 4K default context must be raised. |
| Documented alternative (not the default) | Gemini API free tier | Strong models marked "Free of charge", with tool calling, structured output and an OpenAI-compatible endpoint. | On the free tier, prompts are used to improve Google products and may be read by human reviewers. Free per-model limits are shown only inside AI Studio. |

Sections 3 to 6 give the evidence for each option. Section 8 lists the checks RECAP must run
before any call. Section 9 is the owner's to-do list, and none of it is needed yet.

## 2. Ground rules and host

These come from the master prompt and are not re-cited below:

- The budget is $0. No card, billing upgrade, pay-as-you-go plan or paid fallback.
- A Claude Code subscription is not API credit, so the Anthropic adapter stays disabled.
- A live hosted call needs `RECAP_ALLOW_LIVE_FREE_INFERENCE=1`. If free status cannot be
  verified, RECAP raises an error *before* sending the request.
- CI never makes live calls, and nobody downloads a multi-GB model without approval.

**Host (local check).** Apple M3 with 8 CPU cores and 16 GB of unified memory
(`hw.memsize` = 17,179,869,184 bytes), with 63 GB of free disk. The model identifier is
`Mac15,13`, which Apple lists as MacBook Air (15-inch, M3, 2024) ([Apple model
list][apple-id]). Ollama client 0.34.4 is installed and `llama3.2:3b` is present.

---

## 3. Groq Free plan (recommended primary)

### Verified facts

- **Eligibility and card.** To upgrade "from the Free tier to the Developer tier, you'll
  need to provide a valid payment method". Billing happens only on the Developer tier, at
  month end or at progressive thresholds ($1, $10, $100, ...). Downgrading returns an
  account to "Free tier rate limits and restrictions" and "any usage-based charges stop
  immediately". ([billing FAQ][groq-billing])
- **Free plan limits.** These come from the "Free Plan Limits" tab of the rate-limit page
  ([rate limits][groq-limits]); the status column comes from the [models page][groq-models].

  | Model ID | Status | RPM | RPD | TPM | TPD |
  |---|---|---|---|---|---|
  | `openai/gpt-oss-120b` | production | 30 | 1K | 8K | 200K |
  | `openai/gpt-oss-20b` | production | 30 | 1K | 8K | 200K |
  | `qwen/qwen3.8-27b` | preview | 30 | 1K | 8K | 200K |
  | `openai/gpt-oss-safeguard-20b` | preview | 3 | 1K | 2K | 200K |

- **Models outside the free table.** `llama-3.1-8b-instant` and `llama-3.3-70b-versatile`
  are not in the Free plan table, and the models page lists them as "Enterprise pricing
  (Contact Sales)". Preview models "may be discontinued at short notice". Both
  `gpt-oss` models have a 131,072-token context window. ([rate limits][groq-limits],
  [models][groq-models])
- **Scope.** Limits "apply at the organization level, not individual users". Each project
  has its own quota, capped by the organization's limits. The exact current limits are on
  the Limits page in account settings. ([rate limits][groq-limits],
  [projects][groq-projects])
- **At the limit.** The API returns `429 Too Many Requests`. A `retry-after` header (in
  seconds) is set only on a 429. The `x-ratelimit-*-requests` headers always describe
  requests per day, and the `x-ratelimit-*-tokens` headers describe tokens per minute.
  ([rate limits][groq-limits])
- **Tool calling.** "All models hosted on Groq support tool use." Parallel tool calls are
  **not** supported on the three `openai/gpt-oss` models but are supported on
  `qwen/qwen3.8-27b`. Groq also offers built-in tools (browser search, code execution) and
  remote MCP, both of which run on Groq's servers. ([tool use][groq-tools])
- **Structured output.** With `response_format` type `json_schema` and `strict: true`, the
  output always matches the schema on `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and
  `qwen/qwen3.8-27b`. Strict mode needs every field listed in `required` and
  `additionalProperties: false` on every object. Best-effort mode can fail with HTTP 400.
  The page also says: "Streaming and tool use are not currently supported with Structured
  Outputs." ([structured outputs][groq-so])
- **Model allowlist.** An owner can set "Only Allow" model permissions for the organization
  or a project. A blocked model returns 403 with code `model_permission_blocked_org` or
  `model_permission_blocked_project`. ([model permissions][groq-perms])
- **Spend limits** "are only available on paid plans, not free tier accounts". When a spend
  limit is hit, calls return 400 with code `blocked_api_access`. ([spend limits][groq-spend])
- **Service tiers.** `on_demand` is the default. `performance` is for enterprise plans
  only, `flex` is a Developer-tier benefit, and `auto` uses the best tier available to the
  account. ([service tiers][groq-tiers], [performance tier][groq-perf],
  [billing FAQ][groq-billing])
- **OpenAI compatibility.** The base URL is `https://api.groq.com/openai/v1`. Sending
  `logprobs`, `logit_bias`, `top_logprobs` or `messages[].name` returns a 400, and `n` must
  be 1. ([OpenAI compatibility][groq-openai])
- **API metadata.** `GET /openai/v1/models` returns fields such as `id`, `active` and
  `context_window`. The API reference lists no billing, plan or usage endpoint. The `gpt-oss`
  models accept `reasoning_effort` values `low`, `medium` and `high`, with `medium` as the
  default. ([API reference][groq-api])
- **Data.** "By default, Groq does not retain customer data for inference requests." Any
  customer can turn on Zero Data Retention. ([your data][groq-data])

### Can a paid fallback happen by itself?

- **Verified:** paid usage starts only after an upgrade to the Developer tier with a
  payment method ([billing FAQ][groq-billing]).
- **Inference:** a Free-plan organization with no payment method has no documented path to
  a charge. The realistic risk is a person upgrading the plan.
- RECAP should never send `service_tier` (so the default `on_demand` applies). It should also
  never use Groq's built-in tools or remote MCP, because those run tools outside RECAP's
  safety gateway.

### How a program can check free status

- **Verified:** the API reference has no plan endpoint ([API reference][groq-api]). The plan
  appears only in the console: Settings, then Billing, and the Limits page
  ([billing FAQ][groq-billing], [rate limits][groq-limits]).
- **So RECAP needs two things.** First, an owner confirmation recorded before live use.
  Second, a tripwire on every response. On 2026-10-15 the Free plan allows 1K requests per
  day for `openai/gpt-oss-120b`, while the Developer plan's base is 500K
  ([rate limits][groq-limits]). If `x-ratelimit-limit-requests` does not match the free
  value, RECAP stops and asks the owner to check again.
- **Inference:** a different value means either the organization left the Free plan or Groq
  changed its limits. Both are reasons to stop.

### Inferences and risks

- **Tokens per minute is the binding limit.** A RECAP context pack holds up to 3,000
  estimated tokens (`configs/budget.yaml`). A call also carries the system prompt, the tool
  schemas and the output, and `gpt-oss` models add reasoning tokens. That is roughly 4K to
  6K tokens per call, so expect 1 to 2 calls per minute and about 35 to 50 calls per day
  within 200K TPD. A 30-step run could take 15 to 30 minutes. Use `reasoning_effort: low`
  and short tool descriptions.
- **Tools and JSON schema are separate calls.** Groq cannot combine them in one request, so
  "tool mode" and "JSON mode" stay separate, as the master prompt already asks.
- **Do not make the preview model primary.** `qwen/qwen3.8-27b` is the only free model here
  with parallel tool calls, but it can be withdrawn at short notice.
- **The spare model needs an explicit switch.** Each model has its own row of limits, so
  `openai/gpt-oss-20b` probably has its own quota (inference). Switching to it must be a
  configured, logged choice, never a silent one.

---

## 4. Gemini API free tier (documented alternative)

### Verified facts

- **Eligibility.** "New accounts begin on the Free Tier", and the Free tier needs only an
  "Active project or free trial". Moving to a paid tier means linking a billing account and
  prepaying at least $5. You can unlink a project from billing to return to the free tier,
  and API keys inherit their project's billing status. ([billing][gem-billing])
- **What counts as paid.** Gemini API access is a "Paid Service" only "through a Cloud
  Project associated with an active billing account". Users must be 18 or older, and the
  service is "for professional or business purposes, not for consumer use".
  ([terms][gem-terms])
- **Limits.** The docs publish no free-tier numbers. The limits for each project appear only
  in AI Studio ("View your active rate limits in AI Studio"). Limits apply per project,
  requests per day reset at midnight Pacific time, and experimental and preview models are
  more restricted. Spend-based limits do not apply to the Free tier.
  ([rate limits][gem-limits])
- **At the limit.** The API returns 429 `RESOURCE_EXHAUSTED`. The official SDKs retry 429
  and 5xx errors on their own; the Python SDK retries "up to four times". Do not retry 400,
  402 or 403 errors. ([troubleshooting][gem-trouble])
- **Free models.** On 2026-10-15 the pricing page marks these, among others, "Free of
  charge" on the Free tier: `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`,
  `gemini-2.5-pro`, `gemini-2.5-flash` and `gemini-2.5-flash-lite`. `gemini-4-argon` and
  `gemini-3.1-pro-preview` are "Not available" on the Free tier. ([pricing][gem-pricing])
- **Capabilities.** `gemini-3.8-flash` and `gemini-3.5-flash-lite` are listed as stable and
  support function calling and structured outputs ([models][gem-models],
  [3.8 Flash][gem-38], [3.5 Flash-Lite][gem-35lite]). Function calling has the modes `auto`,
  `any`, `none` and `validated` ([function calling][gem-fc]). Structured output supports a
  subset of JSON Schema, and very large schemas may be rejected
  ([structured output][gem-so]).
- **OpenAI-compatible endpoint.** The base URL is
  `https://generativelanguage.googleapis.com/v1beta/openai/`. It supports function calling,
  structured output and a models list. Support for the OpenAI libraries is "still in beta".
  ([OpenAI compatibility][gem-openai])
- **Data use on the free tier.** Google uses free-tier prompts and responses "to provide,
  improve, and develop Google products", and "human reviewers may read, annotate, and process
  your API input and output". The terms say: "Do not submit sensitive, confidential, or
  personal information to the Unpaid Services." ([terms][gem-terms],
  [pricing][gem-pricing])
- **Regions.** India and the United States are on the list of available regions
  ([regions][gem-regions]). Apps serving users in the EEA, Switzerland or the UK must use
  paid services only ([terms][gem-terms]).
- **Other money traps.** The $300 Google Cloud welcome credit cannot be used for the Gemini
  API ([billing][gem-billing]). Grounding with Google Search or Maps is "Not available" on
  the Free tier. On paid tiers it is charged per 1,000 requests after a monthly free
  allowance ([pricing][gem-pricing]).
- **Checking free status.** In AI Studio, the Projects page shows each project's tier, and
  "Set up billing" means no billing account is attached ([billing][gem-billing]). A program
  can check this through the Cloud Billing API: `projects.getBillingInfo` returns
  `billingEnabled`, which is "True if the project is associated with an open billing
  account". That call needs a Google OAuth credential with a billing scope; the Gemini API
  key alone is not enough. ([getBillingInfo][gcp-billinginfo],
  [ProjectBillingInfo][gcp-pbi])

### Inferences and risks

- **Paid fallback.** A project with no billing account cannot be billed. The risk is that
  the owner's Google account has a billing account, or links one later, on the same project.
  Use a brand-new project dedicated to RECAP. Once billing is linked, tier upgrades happen
  automatically ([rate limits][gem-limits]).
- **Why it is not the default backup.**
  - The free limits cannot be read from the docs.
  - Free-tier data is used for training and may be read by human reviewers.
  - A reliable free-status check needs a second, Google Cloud credential.

  It is fine to enable later if the owner checks the limits in AI Studio and accepts the
  terms. RECAP's simulated incidents contain no personal data, but real cluster logs must
  never go to this tier.
- **Disable SDK retries.** If an SDK is used, turn its automatic retries off, or the
  retries spend the free quota silently.

---

## 5. OpenRouter `:free` variants (recommended backup)

### Verified facts

- **What `:free` is.** A catalog variant: "the free version appears as its own entry in the
  models API with its own pricing, context length, and endpoints". Only models that list a
  free entry support it. ([free variant][or-free])
- **Limits.** Free models allow 20 requests per minute. They allow 50 requests per day if
  the account has bought fewer than 10 credits in total, and 1,000 per day with at least 10.
  Extra accounts or keys "will not affect your rate limits". A negative credit balance can
  cause 402 errors "including for free models". ([limits][or-limits])
- **Fit for production.** The FAQ calls free models "usually not suitable for production
  use" and says "all new users receive a small free allowance" ([FAQ][or-faq]).
- **Checking the key before a call.** `GET https://openrouter.ai/api/v1/key` returns
  `usage` (credits used, all time), `limit`, `limit_remaining`, `is_free_tier`, and
  `free_model_daily_requests` with `used`, `limit` and `remaining`. ([limits][or-limits],
  [key API][or-key])
- **Reading zero prices before a call.** `GET https://openrouter.ai/api/v1/models` returns,
  for each model:
  - `pricing`, as strings in USD per token: `prompt`, `completion`, `request`, `image`,
    `web_search`, `internal_reasoning` and the cache fields;
  - `pricing.overrides`, conditional prices such as long-context or time-based ones;
  - `supported_parameters`, for example `tools`, `tool_choice`, `response_format` and
    `structured_outputs`;
  - `expiration_date`.

  ([models API][or-models])
- **Routing controls.** The `provider` object in a request supports these fields
  ([provider routing][or-routing]):
  - `max_price`, which keeps the request from running when no provider is at or below that
    price;
  - `require_parameters`, which skips providers that do not support every parameter sent;
  - `allow_fallbacks`, `data_collection: "deny"` and `zdr`.

  The `:nitro` and `:floor` suffixes make priority or flex service-tier endpoints eligible
  ([provider routing][or-routing]). Support for structured outputs differs by endpoint
  ([structured outputs][or-so]). Tool-capable models can be filtered with
  `supported_parameters=tools` ([tool calling][or-tools]).
- **Model fallbacks.** These happen only when a request sends a `models` array. A request
  is "priced using the model that was ultimately used", which is returned in the response's
  `model` field ([model fallbacks][or-fallbacks]). A `openrouter/free` router picks a free
  model automatically ([FAQ][or-faq]).
- **At the limit.** The API returns 429 with `error_type: rate_limit_exceeded`.
  `X-RateLimit-*` headers appear only on the error response, and `Retry-After` appears when
  every provider tried returned a hint. A provider-side 429 is retried on other providers of
  the same model automatically. ([limits][or-limits])
- **Data.** OpenRouter does not log prompts by default. Providers that log or train are
  skipped unless the privacy setting allows them ([FAQ][or-faq]). Some free models say their
  provider may train on inputs; Poolside's Laguna S 2.1 is one example
  ([free models][or-free-list]).
- **Snapshot from 2026-10-15** (tool support not checked; do not hardcode these). The free
  collection lists, for example, `nvidia/nemotron-3-super-120b-a12b:free`,
  `nvidia/nemotron-3-ultra-550b-a55b:free`, `poolside/laguna-s-2.1:free` and
  `cohere/north-mini-code:free` ([free models][or-free-list]).

### How a request could reach a paid model

Each of these could send traffic to a paid model:

- requesting the base model ID without `:free`;
- sending a `models` fallback list;
- using the `openrouter/auto` or `openrouter/free` routers, or the `:nitro` or `:floor`
  suffixes;
- a model whose `pricing.overrides` add a price under some conditions.

**Inference:** with zero purchased credits, a paid request should fail with 402 instead of
charging a card ([limits][or-limits]). A new account's small free allowance could still be
spent on a paid model, so RECAP must not rely on that. The fail-closed checks are in
section 8.

### Inferences and risks

- **Small quota.** With no credits bought, 50 requests per day is about one or two full
  RECAP runs. That works for a backup, not for a primary.
- **No fixed model ID.** The free list changes, so RECAP picks the model at setup with an API
  filter and re-checks it before every run.
- **Data policy.** `data_collection: "deny"` may leave no free provider to route to. Whether
  to allow free endpoints that may train on prompts is the owner's choice. RECAP sends only
  simulated data.

---

## 6. Local Ollama (recommended local option)

### Verified facts: API

- **Tool calling.** `/api/chat` takes `tools`; the reply carries `message.tool_calls`. Tool
  results go back as `role: "tool"` messages with `tool_name`. ([API docs][ollama-api],
  [tool calling][ollama-tools])
- **Structured output.** `format` takes either `"json"` or a JSON schema, and "the model will
  generate a response that matches the schema" ([API docs][ollama-api]). The tips for
  reliable output are temperature 0, putting the schema in the prompt as well, and
  validating with Pydantic ([structured outputs][ollama-so]).
- **Other parameters.** `options` (for example `num_ctx` and `temperature`), `stream`,
  `think`, and `keep_alive` (default 5 minutes) ([API docs][ollama-api],
  [chat reference][ollama-chat]).
- **Exact token counts.** The final response reports `prompt_eval_count`, `eval_count` and
  durations in nanoseconds ([chat reference][ollama-chat]). Those give exact token counts
  and a measured speed.
- **Discovery.** `GET /api/tags` lists the models installed locally, and `POST /api/show`
  returns a model's `capabilities` ([API docs][ollama-api]).
- **Context.** The default context window is 4096 tokens; machines with less than 24 GiB of
  VRAM default to 4K, and agent workloads should use more. Set `num_ctx` per request.
  `ollama ps` shows whether a model runs on the GPU or the CPU. ([FAQ][ollama-faq],
  [context length][ollama-ctx])
- **Hardware and concurrency.** Apple GPUs are used through Metal ([GPU][ollama-gpu]). By
  default each model serves one request at a time (`OLLAMA_NUM_PARALLEL` = 1), and memory
  grows with parallel requests times context length. The server listens on 127.0.0.1:11434
  by default. ([FAQ][ollama-faq])
- **Cloud models.** Ollama also offers cloud models that need a sign-in (for example
  `gemma4:cloud`) and have usage plans. Local-only mode is `OLLAMA_NO_CLOUD=1` or
  `"disable_ollama_cloud": true` in `~/.ollama/server.json`. ([cloud][ollama-cloud],
  [FAQ][ollama-faq])

### Models with tool calling that fit 16 GB

Sizes, context windows and capability tags are from the Ollama library tag pages on
2026-10-15 ([llama3.2][ol-llama32], [qwen3][ol-qwen3], [qwen2.5][ol-qwen25],
[llama3.1][ol-llama31], [granite4.1][ol-granite]).

| Tag | Download | Context | Tags | Note |
|---|---|---|---|---|
| `llama3.2:3b` | 2.0 GB | 128K | tools | Installed (local check: model layer 2,019,377,376 bytes) |
| `qwen3:4b` | 2.5 GB | 256K | tools, thinking | Smallest qwen3 with a long context |
| `qwen2.5:7b` | 4.7 GB | 32K | tools | Older generation |
| `llama3.1:8b` | 4.9 GB | 128K | tools | No thinking mode |
| `qwen3:8b` | 5.2 GB | 40K | tools, thinking | **Proposed download.** Ollama's tool-calling guide uses `qwen3` in its examples ([tool calling][ollama-tools]). |
| `granite4.1:8b` | 5.3 GB | 128K | tools | Its page says it supports "tool use, and structured JSON output" |
| `qwen3:14b` | 9.3 GB | 40K | tools, thinking | Borderline on 16 GB (inference) |

**Inference on fit.** Memory holds the model weights, plus a KV cache that grows with
`num_ctx`, plus macOS, RECAP, the dashboard and a browser. An 8B model of about 5 GB at an
8K to 16K context should run fully on the GPU; confirm that `ollama ps` shows "100% GPU".
A 14B model leaves little room. Keep `OLLAMA_NUM_PARALLEL=1`, which means RECAP's two
parallel specialists will take turns on one local model.

### `llama3.2:3b`: what it can and cannot do reliably

**Verified and local check:**

- It has the `tools` tag and a 128K context window. Ollama's page says the 3B model
  outperforms Gemma 2 2.6B and Phi 3.5-mini at instruction following, summarization, prompt
  rewriting and tool use. That is a comparison, not a reliability guarantee.
  ([library page][ol-llama32])
- Its installed chat template (blob `sha256:966de95ca8a6`, the same as the
  [library template][ol-llama32-tpl]) does the following:
  - writes the tool list **only into the last message, and only when that message is a
    user message**;
  - adds the instruction "please respond with a JSON for a function call";
  - passes tool results back in an `ipython` turn;
  - states a knowledge cutoff of December 2023.

**Inference, to be measured in M2:**

- **It can** drive RECAP's real loop end to end: emit tool calls that Ollama parses into
  `tool_calls`, return schema-valid JSON under `format`, and summarize short tool outputs.
- **It cannot be trusted to:**
  - pick the right next tool over many steps;
  - decide well when to stop, because the template pushes it to call a function whenever
    tools are listed;
  - cite the correct evidence IDs;
  - weigh contradicting evidence;
  - resist text injected into logs.
- **The tool list disappears in loops.** After a tool result, the template no longer shows
  the tool list, so each round must end with a short user message (for example "choose the
  next tool or finish") for the tools to appear again.
- **Valid JSON is not a correct answer.** Schema-valid output can still be wrong.
- **Use it** for protocol and plumbing tests, and as a clearly labeled small-model
  baseline, never as evidence of diagnostic quality.

### Expected speed on this M3

- **Verified:** Ollama's docs publish no speed figures (none were found). Apple lists
  100 GB/s of memory bandwidth for this machine's M3 ([MacBook Air M3 15-inch
  specs][apple-mba15]).
- **Inference:** generating each token reads roughly the whole model from memory, so the
  ceiling is about bandwidth divided by model size. That gives at most about 50 tokens per
  second for `llama3.2:3b` (2.0 GB) and at most about 19 for `qwen3:8b` (5.2 GB). Real
  speed will be lower. Reading a 4K to 6K-token prompt adds seconds to every call, and
  `qwen3` thinking adds tokens; use `think: false` where it is acceptable.
- **Measure it, don't assume it:** tokens per second = `eval_count / eval_duration * 1e9`.

---

## 7. Side by side

| | Groq Free | OpenRouter `:free` | Gemini Free | Ollama local |
|---|---|---|---|---|
| Card needed | No (inference: a payment method is asked for only when upgrading) | No (inference) | No: the Free tier is a project with no billing account | No |
| Published free limits | 30 RPM, 1K RPD, 8K TPM, 200K TPD (`gpt-oss-120b`) | 20 RPM; 50 RPD (with fewer than 10 credits ever bought) | Not in the docs; shown only in AI Studio | Your hardware |
| Tool calling | Yes; no parallel calls on `gpt-oss` | Per model; check `supported_parameters` | Yes | Models tagged `tools` |
| JSON schema | Strict on `gpt-oss-20b/120b` and `qwen3.8-27b`; not combined with tools or streaming | Per endpoint | Yes, a subset of JSON Schema | Yes, via `format` |
| Free status checkable by API | No (console only, plus a header tripwire) | Yes: `/api/v1/key` and model prices | Only through the Cloud Billing API (`billingEnabled`) | Not applicable |
| Your data | Not retained by default; ZDR available | Depends on the provider; some train on it | Used to improve Google products; human review | Stays on the machine |
| At the limit | 429 with `retry-after` | 429 (402 if the balance is below 0) | 429 `RESOURCE_EXHAUSTED` | Requests queue; 503 when the queue is full ([FAQ][ollama-faq]) |

The sources for every cell are in sections 3 to 6.

---

## 8. Fail-closed checks RECAP must run before any call

These are proposals for M2. The names of new settings are not final.

**Every hosted provider**

1. **Opt-in.** Refuse unless `RECAP_ALLOW_LIVE_FREE_INFERENCE=1` is set for this process.
2. **Exact allowlist.** The provider ID and model ID must match a configured entry exactly.
   No wildcards and no routers (`openrouter/auto`, `openrouter/free`). No suffix other than
   `:free`.
3. **Fresh proof of free status.** Use API evidence where it exists (OpenRouter). Otherwise
   require a dated owner confirmation that is at most 7 days old (Groq, and Gemini if
   enabled). If the proof is missing or stale, raise an error before the request.
4. **Local budget below the published limit.** Keep per-provider counters of requests and
   tokens per minute and per day in SQLite. Proposed caps: Groq at 25 RPM, 7K TPM, 800 RPD
   and 150K TPD; OpenRouter at 15 RPM and 40 RPD. At a cap, RECAP stops before the provider
   has to say no.
5. **Request shape.** Send only `type: "function"` tools that map to RECAP's read-only MCP
   tools. Never use provider-hosted tools: Groq's built-in tools or remote MCP; Gemini's
   Search or Maps grounding, URL context or code execution; OpenRouter's web search. On
   Groq, omit `service_tier`.
6. **No hidden retries.** Use plain HTTP, or set the SDK's retry count to 0. RECAP allows
   at most one retry after a 429, and only when `retry-after` is short (proposed: 30 seconds
   or less) and the daily budget has room. Otherwise the step is marked `QUOTA_EXHAUSTED` or
   `DEFERRED`.
7. **Stop codes.** On 401, 402 or 403, or on Groq's `blocked_api_access`: stop at once, do
   not retry, and never switch to another provider by itself. A 402 means a billing state
   RECAP did not expect, so it is shown as "free status no longer verified".
8. **Response checks.** The response's `model` must equal the requested model. Record
   `usage` as exact token counts and keep the response headers in the trace.
9. **Data.** Send only simulated incident data to hosted free tiers; never real cluster
   logs, credentials or personal data.
10. **Secrets.** Read API keys only from environment variables. Never log them, and redact
    them from traces, recordings and error messages.

**Groq**

- **Before a run:** `GET /openai/v1/models` shows the model with `active: true`. This is a
  metadata request, not inference.
- **After every response:** `x-ratelimit-limit-requests` must equal the Free plan's daily
  limit for that model (1K for `openai/gpt-oss-120b` on 2026-10-15). Any other value stops
  live use until the owner checks again.

**OpenRouter**

- **Before a run, check the key:** `GET /api/v1/key` must show `is_free_tier: true`,
  `usage == 0` and `free_model_daily_requests.remaining > 0`.
- **Before a run, check the model:** `GET /api/v1/models` must show that the allowlisted ID
  exists and ends in `:free`, and that every `pricing` value is `"0"`. `pricing.overrides`
  must be absent or all zero, and `expiration_date` must be null or in the future.
  `supported_parameters` must contain `tools` for tool mode, or `structured_outputs` or
  `response_format` for JSON mode.
- **In every request:** send
  `"provider": {"max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
  "require_parameters": true}`, and never send a `models` array.
- **After every run:** read `/api/v1/key` again. If `usage` is above 0, disable the provider
  and alert the owner.

**Gemini (only if enabled later)**

- Require the dated owner confirmation that the key's project shows "Set up billing" in AI
  Studio, or `billingEnabled: false` from the Cloud Billing API.
- The model must be marked "Free of charge" on the pricing page at setup.
- Never enable grounding tools.

**Ollama**

- The base URL must be loopback (`http://127.0.0.1:11434`).
- `GET /api/tags` must already list the exact model. RECAP never calls `/api/pull`.
- For tool mode, `POST /api/show` must list `tools` in `capabilities`.
- Refuse cloud model names such as `:cloud` tags, and run the server with
  `OLLAMA_NO_CLOUD=1`.
- Always set `options.num_ctx`. If `prompt_eval_count` comes close to `num_ctx`, treat the
  prompt as possibly cut off and fail that step (inference).

---

## 9. Owner actions needed later (none are needed now)

**Never do any of the following:**

- add a card;
- upgrade Groq to the Developer plan;
- click "Set up billing" in AI Studio;
- buy OpenRouter credits or turn on auto top-up;
- sign in to Ollama's cloud.

**Groq (primary)**

1. Sign up at console.groq.com without adding a payment method.
2. In Settings, Organization, Limits, set model permissions to **Only Allow**
   `openai/gpt-oss-120b` and `openai/gpt-oss-20b` ([model permissions][groq-perms]).
3. Check that Settings, Billing shows the Free plan and that the Limits page shows the
   free-plan numbers.
4. Create an API key and run `export GROQ_API_KEY=...` in the shell. This is the variable
   name in [Groq's quickstart][groq-quick]. Never put the key in a file in the repository.
5. Record the check with a dated confirmation, for example
   `export RECAP_GROQ_FREE_PLAN_CONFIRMED_ON=2026-10-15` (proposed name).
6. Optional: turn on Zero Data Retention ([your data][groq-data]).

**OpenRouter (backup)**

1. Sign up and buy nothing.
2. In the privacy settings, decide whether free endpoints that may train on prompts are
   allowed.
3. Create a key and run `export OPENROUTER_API_KEY=...`.

**Ollama (local)**

1. Approve **one** download: `ollama pull qwen3:8b` (5.2 GB; 63 GB of disk is free).
   `llama3.1:8b` (4.9 GB) is the alternative. Without approval, RECAP stays on
   `llama3.2:3b`.
2. Start the server with `OLLAMA_NO_CLOUD=1 ollama serve`.

**Gemini (optional, only if wanted)**

1. Create a new AI Studio project with no billing account.
2. Check that its Billing Tier column says "Set up billing".
3. Create a key and run `export GEMINI_API_KEY=...`.
4. Accept the free-tier data terms in section 4.

**Every live run**

- Set `RECAP_ALLOW_LIVE_FREE_INFERENCE=1` only in the shell used for that run.

---

## 10. Sources (all read on 2026-10-15)

- **Groq:** [rate limits][groq-limits], [models][groq-models], [tool use][groq-tools],
  [structured outputs][groq-so], [billing FAQ][groq-billing], [spend limits][groq-spend],
  [model permissions][groq-perms], [projects][groq-projects],
  [service tiers][groq-tiers], [performance tier][groq-perf],
  [OpenAI compatibility][groq-openai], [API reference][groq-api], [your data][groq-data],
  [quickstart][groq-quick]. These pages show no last-updated date.
- **Gemini and Google Cloud:** [rate limits][gem-limits] (updated 2026-10-13),
  [pricing][gem-pricing] (2026-10-15), [billing][gem-billing] (2026-09-28),
  [terms][gem-terms] (effective 2026-03-23), [regions][gem-regions] (2026-04-28),
  [OpenAI compatibility][gem-openai] (2026-10-14), [function calling][gem-fc],
  [structured output][gem-so], [models][gem-models], [3.8 Flash][gem-38],
  [3.5 Flash-Lite][gem-35lite], [troubleshooting][gem-trouble],
  [Cloud Billing getBillingInfo][gcp-billinginfo], [ProjectBillingInfo][gcp-pbi].
- **OpenRouter:** [limits][or-limits], [free variant][or-free],
  [provider routing][or-routing], [model fallbacks][or-fallbacks],
  [models API][or-models], [key API][or-key], [FAQ][or-faq],
  [structured outputs][or-so], [tool calling][or-tools],
  [free models collection][or-free-list].
- **Ollama:** [API reference][ollama-api], [chat endpoint][ollama-chat],
  [tool calling][ollama-tools], [structured outputs][ollama-so], [FAQ][ollama-faq],
  [context length][ollama-ctx], [GPU][ollama-gpu], [cloud][ollama-cloud]. Library pages:
  [llama3.2][ol-llama32], [llama3.2 template][ol-llama32-tpl], [qwen3][ol-qwen3],
  [qwen2.5][ol-qwen25], [llama3.1][ol-llama31], [granite4.1][ol-granite].
- **Apple:** [identify your MacBook Air][apple-id],
  [MacBook Air (15-inch, M3, 2024) specs][apple-mba15].

[groq-limits]: https://console.groq.com/docs/rate-limits
[groq-models]: https://console.groq.com/docs/models
[groq-tools]: https://console.groq.com/docs/tool-use
[groq-so]: https://console.groq.com/docs/structured-outputs
[groq-billing]: https://console.groq.com/docs/billing-faqs
[groq-spend]: https://console.groq.com/docs/spend-limits
[groq-perms]: https://console.groq.com/docs/model-permissions
[groq-projects]: https://console.groq.com/docs/projects
[groq-tiers]: https://console.groq.com/docs/service-tiers
[groq-perf]: https://console.groq.com/docs/performance-tier
[groq-openai]: https://console.groq.com/docs/openai
[groq-api]: https://console.groq.com/docs/api-reference
[groq-data]: https://console.groq.com/docs/your-data
[groq-quick]: https://console.groq.com/docs/quickstart
[gem-limits]: https://ai.google.dev/gemini-api/docs/rate-limits
[gem-pricing]: https://ai.google.dev/gemini-api/docs/pricing
[gem-billing]: https://ai.google.dev/gemini-api/docs/billing
[gem-terms]: https://ai.google.dev/gemini-api/terms
[gem-regions]: https://ai.google.dev/gemini-api/docs/available-regions
[gem-openai]: https://ai.google.dev/gemini-api/docs/openai
[gem-fc]: https://ai.google.dev/gemini-api/docs/function-calling
[gem-so]: https://ai.google.dev/gemini-api/docs/structured-output
[gem-models]: https://ai.google.dev/gemini-api/docs/models
[gem-38]: https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
[gem-35lite]: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
[gem-trouble]: https://ai.google.dev/gemini-api/docs/troubleshooting
[gcp-billinginfo]: https://docs.cloud.google.com/billing/docs/reference/rest/v1/projects/getBillingInfo
[gcp-pbi]: https://docs.cloud.google.com/billing/docs/reference/rest/v1/ProjectBillingInfo
[or-limits]: https://openrouter.ai/docs/api/reference/limits
[or-free]: https://openrouter.ai/docs/guides/routing/model-variants/free
[or-routing]: https://openrouter.ai/docs/guides/routing/provider-selection
[or-fallbacks]: https://openrouter.ai/docs/guides/routing/model-fallbacks
[or-models]: https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties
[or-key]: https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key
[or-faq]: https://openrouter.ai/docs/faq
[or-so]: https://openrouter.ai/docs/guides/features/structured-outputs
[or-tools]: https://openrouter.ai/docs/guides/features/tool-calling
[or-free-list]: https://openrouter.ai/collections/free-models
[ollama-api]: https://github.com/ollama/ollama/blob/main/docs/api.md
[ollama-chat]: https://docs.ollama.com/api/chat
[ollama-tools]: https://docs.ollama.com/capabilities/tool-calling
[ollama-so]: https://docs.ollama.com/capabilities/structured-outputs
[ollama-faq]: https://docs.ollama.com/faq
[ollama-ctx]: https://docs.ollama.com/context-length
[ollama-gpu]: https://docs.ollama.com/gpu
[ollama-cloud]: https://docs.ollama.com/cloud
[ol-llama32]: https://ollama.com/library/llama3.2/tags
[ol-llama32-tpl]: https://ollama.com/library/llama3.2:3b/blobs/966de95ca8a6
[ol-qwen3]: https://ollama.com/library/qwen3/tags
[ol-qwen25]: https://ollama.com/library/qwen2.5/tags
[ol-llama31]: https://ollama.com/library/llama3.1/tags
[ol-granite]: https://ollama.com/library/granite4.1/tags
[apple-id]: https://support.apple.com/en-us/102869
[apple-mba15]: https://support.apple.com/en-us/118552
