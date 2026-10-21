# ADR 0017: A fail-closed free-tier guard, and a deferred state when a free quota runs out

**In one sentence:** before every request to a hosted free plan, RECAP checks an explicit
opt-in, an exact model allowlist, a key from the environment, the owner's dated free-plan
confirmation and its own caps (set below the free limits). After every response it compares
the plan's rate-limit header with the free plan's and stops the provider on any mismatch.
When a quota runs out, the run ends `QUOTA_EXHAUSTED` (deferred), never on another provider.

## Status

Accepted (M2, 2026-10-21). Implements the no-spend rules of the master prompt (sections 0.5,
0.6, 0.11 and 3.2) for [ADR 0016](0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md).

## Context

- The budget is $0. A hosted "free" model is acceptable only on an account verified to be
  free, with an allowlist, request caps and fail-closed behavior; free status must be
  verified before a call, never assumed.
- Groq has **no API that reports an account's plan**, and its model list carries no
  rate-limit headers (checked 2026-10-21). Its Free plan has no payment method, so it
  cannot be billed; the realistic risk is a later upgrade to the paid Developer tier.
- Free limits are small (gpt-oss-120b: 30 requests/min, 1,000/day, 8K tokens/min, 200K
  tokens/day), and two parallel agents compete for them.

## Decision

[recap/providers/quota.py](../../recap/providers/quota.py), used by
[FreeHostedProvider](../../recap/providers/openai_compatible.py):

- **Before any request** (including the model-list check), in order, each failure raising
  before anything is sent:
  1. `RECAP_ALLOW_LIVE_FREE_INFERENCE=1` for this process (never set in CI);
  2. the model is exactly one of `free_tier.allowed_models`;
  3. the key's environment variable is set (keys are never read from files);
  4. the owner's confirmation `RECAP_GROQ_FREE_PLAN_CONFIRMED_ON=YYYY-MM-DD` is at most 7
     days old and not in the future (the owner checked the console's billing page);
  5. no earlier tripwire stopped the provider;
  6. RECAP's own caps have room: 25/min, 800/day, 7,000 tokens/min, 150,000 tokens/day,
     40 per run. Counting and reserving happen in one `BEGIN IMMEDIATE` transaction in a
     SQLite ledger shared by every RECAP process.
- **The tripwire, on every response:** `x-ratelimit-limit-requests` (Groq's requests per
  day) must equal the free plan's value for that model (1,000). A different value, a
  missing header, HTTP 402 or `blocked_api_access` stops the provider in the ledger until the
  owner re-checks the billing page and runs `python scripts/providers.py clear-stop groq`.
  The run's cost line then says "cost unknown", never $0.
- **Reservations:** a request reserves its prompt estimate plus `reserved_output_tokens`
  (1,024), not the 2,048-token answer limit: reserving the limit let only one request
  through a minute and starved parallel agents (measured on Groq Free, 2026-10-21). The
  reported count replaces the reservation as soon as the answer arrives; a refusal (401,
  403, 429, 5xx) gives its tokens back.
- **Pacing versus deferral:** a full per-minute cap is a wait (at most 150 s per request,
  bounded by the run's wall clock); a full daily or per-run cap, or the provider's own
  429 with a `retry-after` above 30 s (or a second 429), raises `QuotaExhausted`.
- **`QuotaExhausted` is a `BudgetExhausted`**, so the orchestrator and the controller already
  stop cleanly on it. The controller ends the run in the new terminal state
  **`QUOTA_EXHAUSTED`** ("deferred"), writes no memory episode (a deferred run is not
  experience), and never switches to another provider.
- **Errors that are final:** 401/403 stop the run with no retry; a 5xx is retried once after
  2 s; a schema failure (`json_validate_failed`) is an unusable answer (one corrective retry).
- **The key** is read at send time and put only on the wire (an `Authorization` header);
  recordings, traces, run records, errors and `GET /providers` (which reports only
  "present" or "missing") never contain it. The redactor also knows Groq's `gsk_` format.

## Consequences

### Good

- No request can be sent without the owner's explicit, dated decision, and a plan change is
  caught on the first response after it.
- A quota stop is honest: the dashboard shows "Quota exhausted (deferred)", not FAILED.
- The ledger shows real usage per provider (`GET /providers`, `scripts/providers.py usage`).

### Bad

- The free-plan check rests on the owner's word plus a header comparison. A future Groq
  change to the free limits trips the wire until the configuration is updated (on purpose).
- The very first response after a plan upgrade is the one that reveals it, so that single
  request could be billed on a paid plan. The owner's 7-day confirmation is what keeps this
  from happening.
- Free-plan runs are slow: on 2026-10-21 about two large requests fit in a minute, and a
  run with a parallel round takes several minutes.

## Alternatives considered

- **Trusting "free" model names.** Free catalogs change; the master prompt forbids assuming it.
- **Reserving the full answer limit.** Safe but starved parallel agents (one request a minute).
- **Retrying 429 with backoff until it works.** That drains the free quota and hides the
  limit; one bounded retry, then an honest deferral.
- **Falling back to another provider on 429.** Could reach a paid endpoint; never.

## Amended after the M2 review

- **The tripwire reads every response that carries the header**, whatever its status. A
  paid plan's 400 (`json_validate_failed`) or 429 carries the paid limit too, and a schema
  failure may have generated billed tokens; before, only 200s were compared. A successful
  response without the header still trips it.
- **The ledger is per user, keyed by host.** It lives at `Paths.usage_ledger`
  (`RECAP_USAGE_LEDGER`, default `~/.local/state/recap/llm-usage.db`), outside every data
  folder, and counts and stops by the provider's host (`api.groq.com`), not its name in
  `configs/providers.yaml`. Before, a stop or a daily cap held only inside one
  `RECAP_DATA_DIR`, and renaming the provider entry escaped a stop.
- **The stop is checked inside the reservation's transaction**, so a stop recorded by
  another process between the checks and the request is not missed.
- **Model traffic ignores proxy settings** (`trust_env=False`): a proxy would receive every
  prompt and could answer as "the model", and would turn the loopback-only local model into
  a remote one.
- The owner's confirmation date is compared with the **local** date, the key must be
  printable ASCII (a key pasted with quotes is refused without being echoed), and the
  model-list request runs after checks 1-5 but is not counted against the caps (it is
  metadata).
- A replayed recording makes the same retry choices as the live run did (from the recorded
  settings), without waiting.
- **Still true:** requests already in flight when the tripwire fires (another agent, another
  process) are not stopped by it. The owner's 7-day confirmation is what keeps the account
  free in the first place.
