# ADR 0018: Record every model exchange, and replay real model runs offline in CI

**In one sentence:** every live model request is recorded at the HTTP level (without the
key), and a `replay` provider answers from a recording through the same provider code, so
CI runs an authentic model-driven incident offline at $0, always labeled `REPLAY`, and a
stale recording fails loudly instead of guessing.

## Status

Accepted (M2, 2026-10-21). Complements
[ADR 0016](0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md).

## Context

- CI must never call a hosted model or need a local one, yet the LLM path (prompt
  rendering, schemas, parsing, harness, verifier, approval, executor) must be tested with
  real model output, not only with scripted fakes.
- Debugging a free-tier run should not spend more free requests.
- The master prompt requires replays to carry provenance (original provider, model,
  recording ID, timestamp, schema version) and never to be shown as live responses.

## Decision

- **One transport seam** ([recap/providers/transport.py](../../recap/providers/transport.py)):
  providers hand a JSON body to a `ModelTransport` and get back an `HttpExchange`.
  `LiveTransport` (httpx, one request per call), `RecordingTransport` (live plus a JSONL
  recording) and `ReplayTransport` (answers from a recording, no network).
- **Recordings** (`data/recordings/<id>.jsonl`): a header (recording ID, provider, model,
  prompt version, RECAP version, the provider's settings, which hold env var NAMES only),
  then one line per exchange with the request body and the response. Before writing,
  metadata responses are trimmed to what the provider reads (Ollama's `/api/show` also
  returns a license, a template and a local file path) and every line is scrubbed for
  secret values, key formats and the user's home folder.
- **Matching by fingerprint, not order:** a fingerprint is the SHA-256 of the method, path
  and canonical body with run-specific random values normalized (run, incident, proposal,
  episode IDs and the 6-character run prefix of evidence IDs). Each recorded answer is used
  once. When a recorded answer cites evidence, its run prefix is swapped for the current
  run's, found as the prefix that appears most often in the request (the system prompt's
  one example ID loses to the run's own IDs).
- **The replay provider** (`RECAP_PROVIDER=replay`, recording from
  `configs/providers.yaml` or `RECAP_REPLAY_RECORDING`) rebuilds the recorded provider from
  the header's settings and gives it a `ReplayTransport`. Its info says `kind: replay`,
  `origin: replay`, `name: replay:ollama:qwen3:8b` and carries `replay_of`. A replayed
  answer adds no tokens and no model requests to the run's usage (its `llm.call` events keep
  the original counts, marked `replayed`). A hosted recording replays without the free-tier
  guard, because nothing is sent.
- **The committed fixture** `fixtures/recordings/ollama-qwen3-8b-bad-deployment.jsonl` is a
  real `qwen3:8b` run; `tests/e2e/test_recorded_model_runs.py` replays it through the whole
  system and checks the outcome, the model's tool choices and proposal, and that the file
  holds no secrets or local paths.

## Consequences

### Good

- CI exercises a real model's decisions end to end with no network, no model and no cost.
- A recording shows exactly what was sent to the model and what came back, for debugging
  without another request.
- A changed prompt, schema or tool result produces a different fingerprint, so a stale
  recording is detected (`ReplayMismatch`) rather than silently reused.

### Bad

- Any change to prompts-v4, the schemas or the simulator's tool output needs a re-recorded
  fixture (about 2-4 minutes of local inference).
- Replay depends on the run asking the same questions. Parallel agents that call tools at
  overlapping times can number evidence differently from the recording; the replay then
  fails with `ReplayMismatch` (never a wrong answer). The committed fixture resolved in one
  single-agent round.
- Recordings contain prompts and model output: fine for simulated data, but recordings of a
  real cluster would need their own review before sharing.

## Alternatives considered

- **Recording decisions instead of HTTP exchanges.** A replay would then skip the parsing
  and validation code, the part most worth testing.
- **Matching requests by order.** Breaks as soon as two agents run concurrently.
- **Mocking the model in CI.** Scripted fakes test the plumbing (they are used too:
  `tests/llm_fakes.py`) but not a real model's choices.

## Amended after the M2 review

- **Recordings are redacted as data, then serialized.** Redacting the serialized line could
  cut through a JSON escape when a log line said `token="..."`, leaving a recording that no
  longer parsed; untrusted text could break replay that way. The redactor's free-text rule
  also no longer swallows a backslash. A recording that does not parse is a `ConfigError`.
- **Replays never count usage**, including a replayed schema failure, and make the same
  retry choices as the live run without sleeping.
- **A replay runs with the recorded run's limits** (`budget_overrides` from the recording's
  settings), so every prompt says the same "step N of M".
- **A `ReplayMismatch` in one parallel agent stops the whole run**, like a budget stop; so
  does the free-plan tripwire (`FreeStatusLost`). A quota stop wins over an ordinary budget
  stop whatever the agents' order, so the run is deferred.

## Amended after the human-approved run review

- Recordings no longer keep `x-request-id`, nor a hosted answer's own IDs (`id`, Groq's
  `x_groq`, `system_fingerprint`): they could link a published recording to the provider
  account. A chat answer keeps only the fields the provider reads, and the rate-limit headers
  the tripwire and replays need.
- `/api/show` keeps the weights file's name (`FROM sha256-…`, never its path), so a replay
  knows which weights answered the original run.
- `scripts/export_run_trace.py` now publishes a run's recording with its trace, so a shared
  run can be replayed offline by anyone with the same RECAP code
  ([ADR 0019](0019-checkable-evidence-of-a-real-model-run.md)).
- The committed fixture was re-recorded with prompts-v4.2 (5 requests, 12,891 / 744 tokens,
  RESOLVED in one round).
