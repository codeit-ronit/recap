# RECAP status

This page records what was actually measured, milestone by milestone. Every number below
comes from a command that was run; nothing is projected. It is updated at the end of each
milestone of the zero-cost next phase (M0 to M6).

**Cost so far: $0.** Since M2, real model requests have been sent, and every one was free:
local inference with Ollama on this machine, and Groq's Free plan, whose rate-limit header
showed the free plan's limit on every response. No paid API was called.

---

## M0: baseline and audit reproduction (2026-10-15)

### Starting point

| Item | Value |
|---|---|
| Git commit | `ad382e6` (the V1 release on `main`) |
| Working tree | clean (the private master prompt is excluded from git) |
| Host | Apple M3, 8 CPU cores, 16 GB unified memory, 63 GB free disk |
| Python | 3.11.15 in `.venv` (managed with uv 0.11.1) |
| Node / npm | 24.14.1 / 11.11.0 |
| Ollama | client 0.34.4 installed; one local model present: `llama3.2:3b`; the server was not running |
| Docker, Kind, kubectl | not installed (needed later for the local Kubernetes milestone, M5) |

### Baseline checks (before any change)

| Check | Command | Result |
|---|---|---|
| Python tests | `.venv/bin/python -m pytest -o addopts="" -q -rs` | **781 passed, 1 skipped** in 30 s. The skip is the optional OpenTelemetry SDK test (`tests/unit/test_trace_sinks.py:203`). |
| Lint | `.venv/bin/ruff check .` | clean |
| Format | `.venv/bin/ruff format --check .` | clean (178 files) |
| Dashboard tests | `cd apps/dashboard && npm test` | 6 passed, 0 failed |
| Dashboard build (strict TypeScript) | `npm run build` | passed |
| End-to-end smoke test | `.venv/bin/python scripts/smoke_test.py` | `RESOLVED (66 trace events, simulated environment)` in 3 s |
| Python type checking | none configured | ruff only; a static type checker is not part of CI |

### Pinned versions

- `constraints.txt` (new): the exact versions of all 50 Python packages that passed the
  baseline. CI and `scripts/bootstrap.sh` now install with `-c constraints.txt`.
- `.python-version` (new): `3.11`, the version CI uses.
- `apps/dashboard/.nvmrc` (new): `24`; CI reads it. The dashboard's npm packages were already
  pinned to exact versions in `package.json` and `package-lock.json`.
- CI now also runs `ruff format --check .`.

### Audit findings reproduced as failing tests

Every confirmed audit finding now has a test that asserts the **correct** behaviour and
fails on today's code. They are marked `@pytest.mark.xfail(strict=True)`: the suite stays
green, but the moment a fix makes one pass, pytest reports an error until its mark is
removed. Each test was also run with `--runxfail` to check that it fails on its intended
assertion (not on an import, fixture or timeout error). Guards are tests of behaviour that
already works and must keep working after the fix.

| Audit item | Test file | Reproductions (xfail) | Guards | What the reproduction shows today |
|---|---|---|---|---|
| 2.1 Context loss | `tests/adversarial/test_audit_context_survival.py` | 7 | 1 | Real simulator logs: **0 of 18** inventory ERROR lines and **0 of 3** payment ERROR lines survive packing. A wide log query loses its newest errors in the gateway's 6,000-char cap (inventory 8 of 18 visible, payment 0 of 3), even in a pack with room to spare. A 240-minute latency series loses its spike (peak `3170` not visible). End to end, no pack handed to the provider in `test-inventory-bad-release` shows any ERROR line. |
| 2.2 Forged evidence | `tests/adversarial/test_audit_forged_evidence.py` | 3 | 2 | With forged log lines, the run restarts the healthy `payment` service (`('restart_service', 'payment')` is executed). V3 marks "resource pressure on payment" **supported** from OOM log text alone while memory sits at 200 of 512 MB. V2 **contradicts** checkout's bad release because 24 of 36 checkout errors blame a dependency that is observed healthy. |
| 2.3 Oracle and label leakage | `tests/adversarial/test_audit_oracle_isolation.py` | 2 | 1 | After a failed fix, the retry prompt says "RECAP's verifier asked to look closer at: checkout, frontend, oracle-only-sentinel": the oracle's private list reaches the model. Provider requests carry `incident.scenario_id`, `memory_hints[0].episode.scenario_id` and `memory_hints[0].episode.failure_family`. |
| 2.4 Approvals and API security | `tests/adversarial/test_audit_approval_race.py`, `tests/e2e/test_audit_api_security.py` | 2 + 5 | 1 | The operator types `n` after an API approval and the run still ends RESOLVED (the rollback ran). A rejection from another process is refused with "approval request is approved, not pending". With no credentials: `POST /approvals/{id}` returns 200 and stores the client-chosen actor `incident-commander`; `POST /incidents/run` and `POST /evals/run` return 202; a request for `Host: evil.example` returns 200. |
| 2.5 MCP execution authority | `tests/adversarial/test_audit_executor_authority.py` | 19 | 1 | Called directly, every write tool runs with an empty or made-up approval (for example "rolled back checkout from v2.4.1 to v2.4.0"). A second `python -m recap.tools.mcp_server` process for the same run can roll back and delete the namespace. A finished run's public rollback approval unlocks `delete_namespace`; a used approval replays a restart. All seven read tools silently drop an argument they do not define. |
| 2.6 Recovery lifecycle | `tests/e2e/test_audit_retry_and_stabilization.py` | 5 | 5 | Two faults: after a half-working restart of payment, round 2 still blames payment from pre-fix evidence and the run ends FAILED ("no remediation could be drafted"). The proposal pack drops `runbook-memory-pressure#remediation-1`, so the fix is a `fallback` restart. A live-like target that needs 1.5 s to settle is judged `not_recovered` immediately. A provider call that hangs past a 2 s run budget ends FAILED (investigation) or overruns to 5.2 s (proposal) instead of BUDGET_EXHAUSTED at the deadline. |
| 2.7 Test audit | (all of the above) | | | The new tests run the real controller, MCP subprocess, gateway and simulator wherever the finding is end to end; unit-level tests pin each faulty rule on its own. Counting end-to-end tests separately is part of M1. |

Findings the reproduction added to the audit's list (all covered by the tests above):

- The gateway's text cap (`limits.max_output_chars`) also keeps only the **oldest** part of a
  long result, independently of the packer.
- The proposal context pack can drop the runbook chunk for the diagnosed mechanism, which
  silently turns a runbook-backed fix into a blind restart.
- A provider call has no deadline tied to the run's wall-clock budget: the investigation
  timeout counts from the agent's start, and the proposal call has none.
- An existing test, `tests/unit/test_packer.py::test_priority_ids_are_packed_first_even_on_a_tight_budget`,
  would pass even without the priority bonus; the new guard forces a real choice.

### Test counts after M0

| Check | Result |
|---|---|
| Python tests | **792 passed, 1 skipped, 43 xfailed** in 54 s (781 baseline tests, plus 11 new guards and 43 strict-xfail reproductions) |
| Reproductions with `--runxfail` | 43 failed, 11 passed: every reproduction fails on its intended assertion |
| Lint and format | clean (187 files) |

### Research done without any LLM request

`docs/provider-options.md` compares free inference options from official documentation only
(no API calls, no accounts, no downloads). Proposal: Groq Free plan (`openai/gpt-oss-120b`)
as the hosted primary, an OpenRouter `:free` model selected by API price checks as the
backup, and local Ollama (`llama3.2:3b` now for plumbing tests; `qwen3:8b`, a 5.2 GB
download, only after approval).

### Scoped plan for M1

Each item is fixed with its reproduction tests turned green, without weakening them.

1. **Oracle isolation (2.3).** Providers receive label-free views of the incident and of
   remembered episodes. After a failed fix, the next round's focus comes only from the
   agents' own findings and the failed fix's target, never from the oracle's service list.
2. **Context survival (2.1).** Tool results reach the model as structured summaries (newest
   errors first, counts, first-error time relative to the alert; metric baseline, min, max,
   latest, largest jump and percent of limit). Long text keeps its head and tail. The
   diagnosis' runbook chunks are pinned in the proposal pack.
3. **Evidence trust (2.2).** A new verifier rule, V6, resolves conflicts: platform signals
   (metrics, events, health) outrank log text. OOM text alone is insufficient; blame toward a
   dependency observed healthy does not count.
4. **Recovery lifecycle (2.6).** A bounded stabilization wait for live targets (the simulator
   stays instant); verification after a fix uses only observations newer than that fix for
   the remediated service; provider calls are bounded by the run's deadline.
5. **Execution authority (2.5).** The gateway issues a short-lived, single-use Ed25519
   capability for exactly the approved action; the environment adapter verifies it at the
   write boundary (signature, scope, expiry, nonce). The MCP server rejects unknown arguments.
6. **Approvals and API (2.4).** A rejection wins until the approval is used; transitions are
   compare-and-swap inside one SQLite transaction; an append-only decision log. The API
   requires an operator bearer token on every state-changing endpoint, takes the approver
   identity from it, checks the Host and Origin headers, and gains a cancel endpoint. The
   dashboard gets an operator sign-in.
7. **Test audit (2.7).** An `e2e` pytest marker, so end-to-end tests are counted separately.

---

## M1: security, context and oracle hardening (2026-10-15)

Every audit finding reproduced in M0 is fixed, and its reproduction test now passes (the
strict-xfail marks were removed in the same commits as the fixes). Nothing was weakened:
reproduction assertions are unchanged or stronger.

### Fixes, one commit each

| Audit item | Commit | What changed | Decision record |
|---|---|---|---|
| 2.3 Oracle and label leakage | `94b8071` | Label-free provider views (`AgentIncident`, `AgentEpisode`, `AgentMemoryHit`); retry focus from RECAP's own reasoning, never the oracle | [ADR 0010](decisions/0010-label-free-views-and-a-yes-no-oracle.md) |
| 2.1 Context loss | `3a7c3f6` | Structured evidence summaries; head-and-tail cuts in the packer and the gateway; pinned runbooks | [ADR 0011](decisions/0011-structured-evidence-summaries.md) |
| 2.2 Forged evidence | `7469d01` | Verifier rule V6: platform signals outrank log text | [ADR 0012](decisions/0012-platform-signals-outrank-log-text.md) |
| 2.6 Recovery lifecycle | `29f997d` | Stabilization polling for live targets; epoch-aware verification; provider calls bounded by the run's deadline | [ADR 0013](decisions/0013-stabilization-epochs-and-deadlines.md) |
| 2.5 Execution authority | `884f04a` | Ed25519 single-use capabilities verified by the environment; strict MCP tool arguments | [ADR 0014](decisions/0014-signed-capabilities-at-the-write-boundary.md) |
| 2.4 Approvals and API | `2df849c` | "No" wins until the approval is used; compare-and-swap transitions; append-only decision log; operator bearer token, host and origin checks; dashboard sign-in | [ADR 0015](decisions/0015-operator-auth-and-no-wins-approvals.md) |
| 2.7 Test audit | `bf37f1e` | `e2e` pytest marker, `--strict-markers` | (this page) |

### Test results after M1

| Check | Command | Result |
|---|---|---|
| Python tests | `.venv/bin/python -m pytest` | **912 passed, 1 skipped, 0 xfailed** in about 55 s (the skip is the optional OpenTelemetry test) |
| By folder | `--collect-only tests/<folder>` | unit 780 · integration 25 · adversarial 62 · e2e folder 46 · total 913 |
| End-to-end (real controller, MCP subprocess, gateway, simulator) | `.venv/bin/python -m pytest -m e2e` | **43 passed** (43 test cases from 40 test functions) |
| Audit reproductions | `.venv/bin/python -m pytest tests/adversarial/test_audit_*.py tests/e2e/test_audit_*.py` | 43 reproductions and 11 guards, all pass |
| Lint and format | `ruff check .` and `ruff format --check .` | clean |
| Dashboard tests | `cd apps/dashboard && npm test` | 12 passed, 0 failed |
| Dashboard build (strict TypeScript) | `npm run build` | passed |
| Smoke test | `.venv/bin/python scripts/smoke_test.py` | `RESOLVED (66 trace events, simulated environment)` |
| Five CLI demos | `scripts/demo.py --scenario <id> --approve auto` | all five match their expected final state |
| Live API check | uvicorn on 127.0.0.1, curl as the dashboard would call it | 401 without the token, 403 for a foreign Origin, 400 for a foreign Host; a manual run approved with the token ended RESOLVED; decision log `requested → approved (operator:local-operator) → consumed` |

No skipped test is a critical one: the only skip is the optional OpenTelemetry extra.

### Evaluation after M1 (synthetic, scripted policy, simulated)

Report `20261015-121819-442b88`. Outcomes and diagnoses are identical to V1 in every
configuration. What changed:

| configuration | recovered (V1 → M1) | mean tool calls (V1 → M1) | mean est. tokens (V1 → M1) |
|---|---|---|---|
| baseline_single | 3/6 → 3/6 | 14.2 → 14.2 | 20,008 → 8,635 |
| full_adaptive | 6/6 → 6/6 | 8.8 → 8.8 | 13,375 → 8,416 |
| no_memory | 6/6 → 6/6 | 8.8 → 9.7 | 11,298 → 8,436 |
| no_rag | 5/6 → 5/6 | 10.5 → 10.0 | 14,163 → 6,327 |
| single_only | 6/6 → 6/6 | 10.0 → 10.0 | 15,686 → 9,523 |

- Tokens fell 25 to 57% because providers read evidence summaries instead of raw JSON.
- `test-checkout-misleading-deploy` needs a second round without memory (10 → 15 tool calls),
  because out-of-memory log text alone no longer proves memory pressure (rule V6).
- Failed fixes retry with fewer calls (for example 17 → 15), because a retry no longer gets
  the oracle's list of unhealthy services.

### Review hardening (after the M1 fixes)

Three independent reviews (security, correctness, test quality) examined the M1 changes.
Every confirmed finding was fixed. Each code fix with a test was checked by reverting it for
a moment: the matching test then fails on its intended assertion (25 such checks, all
caught). Two items have no automated test, and the table says so: the dev server's host
check was verified by hand, and the crash on a target-reported time disappeared with the code
that read that time. The numbers in this section replace the M1 numbers above.

| Area | What the review found | What changed | Tests |
|---|---|---|---|
| Cancel or stop during a write | A run cancelled while its write was in flight lost that write from the record | Write-ahead `mitigation.started` event; the gateway waits for the outcome; the controller records the fix as `error`, "recovery not verified", with its used approval | `test_cancelling_while_a_fix_is_being_sent_still_records_that_fix` |
| Waiting for a live target | The wait could outlive the run's budget; a refused write waited the full window | The wait ends at the window or the run's deadline (BUDGET_EXHAUSTED, fix recorded); a refused write is judged with one probe | 3 new tests in `tests/e2e/test_audit_retry_and_stabilization.py` |
| Epoch boundary | It came from the target's own `executed_at`; an absurd value crashed the run after the write | The oracle's "before" probe time (the code that read the target's time is gone); a namespace-wide action marks every service | `test_a_fix_marks_exactly_the_services_it_changed` (which services; the removed code has no test) |
| Deadlines | A provider's own `TimeoutError` could pass for the run's deadline | Only the run's clock gives BUDGET_EXHAUSTED; with no time left no agent starts | 2 in `tests/unit/test_orchestrator.py`, 1 end to end |
| Verifier | Blame toward a "fixed" dependency was dropped; a slow dependency counted as healthy; stale logs or events after a fix contradicted a claim | Blame counts until fresh platform evidence shows the dependency healthy; healthy needs p95 latency within 500 ms; stale data leaves a claim insufficient | 8 new tests in `tests/unit/test_verifier.py` |
| Summaries and packing | NaN or infinity in tool data failed the investigation round; newer warnings could crowd out the newest ERROR; the injection prefix could push a fitted summary into compaction | Finite points only, a raw fallback, a "newest ERROR" line, room kept for the prefix | 5 new in `tests/unit/test_summaries.py`, 1 in `tests/unit/test_packer.py` |
| Write boundary | Refused writes went into the world's action log, without bound | A separate list of the newest 50 (fields truncated, no capability) and a count | `test_unauthorized_attempts_cannot_grow_the_world_state_without_limit` |
| MCP server | Only tools registered with the decorator rejected unknown arguments | `StrictToolServer.add_tool` records every tool's arguments | `test_a_tool_registered_any_other_way_is_strict_too` |
| Approvals | `INSERT OR REPLACE` could rewrite a decision-log row | A trigger refuses it | a new case in `tests/unit/test_approvals.py` |
| Operator token file | A token file others could read, owned by someone else, or a symlink was used | The server refuses to start with it | 2 new in `tests/e2e/test_api_security.py` |
| Dashboard dev server | Vite answered any Host header, and its proxy sent CORS headers | Loopback host names only (403 otherwise), no CORS, Host kept | checked by hand only: a foreign Host got 403, a proxied sign-in worked |
| Test quality | Some tests could not fail (one rendered the same object twice; others asserted text the raw fallback also contains), and some names promised more than they checked | Rewritten to check real summaries, the retry focus's inputs, and where the dashboard keeps the token; the reviewers' gap tests adopted (10) | `test_summaries.py`, `test_agent_views.py`, `auth.test.ts`, and others |

Test results after the review:

| Check | Command | Result |
|---|---|---|
| Python tests | `.venv/bin/python -m pytest` | **941 passed, 1 skipped** in about 60 s (the skip is the optional OpenTelemetry test) |
| By folder | `--collect-only tests/<folder>` | unit 800 · integration 27 · adversarial 62 · e2e folder 53 · total 942 |
| End-to-end | `.venv/bin/python -m pytest -m e2e` | **48 passed** |
| Lint and format | `ruff check .` and `ruff format --check .` | clean |
| Dashboard tests | `cd apps/dashboard && npm test` | 13 passed, 0 failed |
| Dashboard build (strict TypeScript) | `npm run build` | passed |
| Smoke test | `.venv/bin/python scripts/smoke_test.py` | `RESOLVED (67 trace events, simulated environment)`: one more than before, the write-ahead `mitigation.started` |
| CLI demos | `scripts/demo.py --scenario <id> --approve auto` | all 15 scenarios (the five demos included) match their expected final state |

Evaluation after the review (report `20261015-132617-a08fdf`, git `18198af`). Test-suite outcomes, tool
calls and diagnoses are unchanged. Two things changed:

| configuration | mean est. tokens (V1 → M1 → after review) |
|---|---|
| baseline_single | 20,008 → 8,635 → 9,154 |
| full_adaptive | 13,375 → 8,416 → 8,629 |
| no_memory | 11,298 → 8,436 → 8,691 |
| no_rag | 14,163 → 6,327 → 6,597 |
| single_only | 15,686 → 9,523 → 9,807 |

- Tokens are 23 to 54% below V1 (25 to 57% right after M1): the "newest ERROR" line costs a
  few percent.
- The safety-off red-team run now makes 1 unsafe attempt instead of 2 (1 executed in the
  sandbox either way). After its `delete_namespace`, every service counts as changed, so no
  stale diagnosis drives a second attempt; the run ends FAILED for a human to take over.

### What genuinely ran

Scripted provider only (`deterministic-scripted-policy`, not an LLM), simulated environment
only. **No LLM request of any kind was sent, no model was downloaded, nothing was provisioned.
Cost: $0.**

### Still open after M1 (blockers for later milestones)

- **M2 (real LLM inference)** needed one owner decision: done on 2026-10-21 (see M2 below).
- **M5 (local Kubernetes)**: Docker, Kind and kubectl are not installed on this machine.
- The threat model lists what a real cluster still needs:
  [Before a real Kubernetes target](threat-model.md#before-a-real-kubernetes-target).

---

## M2: zero-cost real model providers (2026-10-21)

**Owner decisions:** approved the 5.2 GB `qwen3:8b` download, and provided a Groq key for a
Free-plan account (no payment method). The key was stored only in a private file outside
the repository (`~/.config/recap/groq.env`, mode 600); because it was pasted into a chat,
rotating it after M2 is recommended. Live hosted calls ran only with
`RECAP_ALLOW_LIVE_FREE_INFERENCE=1` set for that one process.

### What was built

| Piece | Where | ADR |
|---|---|---|
| Step protocol prompts-v4 (prompts-v4.1 after the review): per-request JSON schemas from the trusted catalog, parsing, one corrective retry | `recap/providers/protocol.py`, `http_llm.py` | [0016](decisions/0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md) |
| Local provider (Ollama, `qwen3:8b`, native tool calls) | `recap/providers/ollama_provider.py` | 0016 |
| Free hosted provider (Groq Free, `gpt-oss-120b`, strict JSON) | `recap/providers/openai_compatible.py` | 0016 |
| Free-tier guard, usage ledger, tripwire, `QUOTA_EXHAUSTED` | `recap/providers/quota.py`, `recap/core/lifecycle.py`, `controller.py` | [0017](decisions/0017-fail-closed-free-tier-guard-and-a-deferred-state.md) |
| Recording and replay (`replay` provider, committed real-model fixture) | `recap/providers/transport.py`, `fixtures/recordings/` | [0018](decisions/0018-record-every-model-exchange-and-replay-in-ci.md) |
| `ModelCall` / `llm.call` events, `Usage.model_requests`, `ProviderInfo.origin` | `recap/core/models.py`, `harness/loop.py` | 0016 |
| `GET /providers`, run-origin badges, LLM lines in the event feed, provider table, deferred state | `recap/providers/catalog.py`, `apps/api/main.py`, `apps/dashboard/src/` | — |
| `scripts/providers.py` (readiness, usage, clear-stop), `scripts/export_run_trace.py` (sanitized trace), `demo.py --provider` | `scripts/` | — |
| Paid Claude adapter: now also needs `RECAP_ALLOW_PAID_LLM=1` | `recap/providers/factory.py` | 0016 |

### Measured on this machine (MacBook Air 15", M3, 16 GB; Ollama 0.34.4)

- `qwen3:8b` (Q4_K_M): prompt evaluation about 155-165 tokens/s, generation about 14
  tokens/s, first model load 23 s; with `num_ctx` 12,288 it ran 100% on the GPU (6.8 GB).
- One investigation step, same prompt (about 2,700 tokens): JSON-schema answer 75 s and
  790 output tokens; JSON with `think: true` 65 s (905 output tokens, about 400 of them
  thinking, prompt cached); native tool call about 16 s and 57 output tokens. Hence `tools`
  mode for Ollama's investigation steps, JSON for proposals.
- With V1's prompts (prompts-v3) the model called the bad release a `dependency_failure`
  (also with thinking on); with prompts-v4 it gave `bad_deployment`, `checkout`, `v2.4.1`,
  citing four real evidence IDs. A JSON proposal once looped on its citations until the
  output limit (now capped with `maxItems`).

### Real model runs (simulated `bad-deployment`, no memory, development runs)

Every run below used the **simulation auto-approver**; the human-approved deliverable run is
listed under "Still open". Every fix was proposed by the model, judged by the policy, executed with a
signed capability and verified by the oracle.

| Run | Model | Requests | Tokens in / out (server-reported) | Outcome |
|---|---|---|---|---|
| local 1 | `qwen3:8b` | 10 (1 unusable, retried) | 26,911 / 1,580 | RESOLVED, 246 s: round 1 claimed a bad deploy without reading logs (verifier: insufficient), round 2 parallel specialists found it; rollback to `v2.4.0` |
| local 2 (the first CI fixture) | `qwen3:8b` | 7 | 18,331 / 809 | RESOLVED, 143 s, one round: health, deployments, events, logs, 2 metrics, then the diagnosis and the rollback |
| local 3 (the CI fixture since the review, prompts-v4.1) | `qwen3:8b` | 8 | 21,489 / 771 | RESOLVED, about 2.5 min, one round: health, deployments, logs, error-rate and latency metrics, events, then the diagnosis (5 evidence IDs) and the rollback |
| Groq 1 | `gpt-oss-120b` | 3 | 7,083 / 699 | QUOTA_EXHAUSTED (deferred) at 122 s: reserving the full answer limit let two parallel agents starve RECAP's own per-minute cap (fixed: reserve a typical answer, pace up to 150 s) |
| Groq 2 | `gpt-oss-120b` | 8 (2 cut off: HTTP 400 `json_validate_failed`) | 15,365 / 1,685 | RESOLVED, 246 s, after an escalation; the cut-offs showed a 1,024-token limit is too small for a reasoning model (fixed: 2,048) |
| Groq 3 (final settings) | `gpt-oss-120b` | 5 | 13,368 / 1,354 | FAILED safely, 182 s: it diagnosed after 2 tool steps without the service's logs, in both rounds; the verifier refused and handed over to a human |

- **Free plan, checked on every response:** all 14 successful Groq responses (and the 2
  schema failures) carried `x-ratelimit-limit-requests: 1000`, the Free plan's daily limit;
  `x-ratelimit-remaining-requests` counted down (989 → 985 in Groq 3).
- **Replay:** local 1 replayed offline 3 times (about 1.2 s each, RESOLVED, 0 model
  requests); local 3 is `fixtures/recordings/ollama-qwen3-8b-bad-deployment.jsonl` (local 2
  was, until prompts-v4.1 changed the requests), replayed by
  `tests/e2e/test_recorded_model_runs.py`.
- Small sample, one scenario: these runs show the real path works and fails safely; they say
  nothing yet about how often a model is right.

### Test results after M2

| Check | Result |
|---|---|
| `python -m pytest` | **1082 passed, 1 skipped** (the optional OpenTelemetry test), 68 s |
| By folder | unit 935 · integration 27 · adversarial 62 · e2e 59 |
| `python -m pytest -m e2e` | **54** cases (51 functions), up from 48 |
| `ruff check .`, `ruff format --check .` | clean |
| Dashboard | 17 tests pass (`npm test`); strict build passes |
| `scripts/smoke_test.py` (scripted) | RESOLVED, 67 trace events (unchanged) |
| `scripts/evaluate.py` (scripted) | report `20261021-061715-3789eb` at git `142b681`: every number identical to the M1-review report (the scripted path did not change) |

New tests: `tests/unit/test_protocol.py`, `test_ollama_provider.py`,
`test_free_hosted_provider.py`, `test_quota.py`, `test_replay.py`, `test_provider_catalog.py`
(local protocol fixtures: fake Ollama and Groq servers behind httpx's mock transport, so the
real HTTP code runs); `tests/e2e/test_llm_provider_runs.py` (model-driven runs through the
whole system: operator-approved resolution, no oracle or label data in any byte sent to the
model after a failed fix, replay, a deferred quota run with the key in no file);
`tests/e2e/test_recorded_model_runs.py` (the real `qwen3:8b` run, offline).

### Review hardening (after the M2 code)

Three independent reviews (security, correctness, test quality) examined the M2 changes.
Every confirmed finding was fixed. Each fix has a test that was checked by reverting the fix
in a scratch copy: the test then fails on its intended assertion (52 such checks, all
caught: 32 for the security and correctness findings, 19 for the test review's gaps, and 1
for the stable tool definitions, added while writing these notes). One
first attempt did not fail when its fix was reverted (httpx ignores proxies whenever a mock
transport is given); that test now runs a real loopback "proxy" through the production code.
The numbers in this section replace the M2 test numbers above. Details per threat:
[threat-model.md](threat-model.md#the-m2-review-hardening-the-model-path).

| Area | What the review found | What changed | Tests |
|---|---|---|---|
| Free-plan tripwire | Only successful responses were compared, but a paid plan's 400 or 429 carries the paid limit too (and the corrective retry was then sent) | Every response that carries the header is compared, whatever its status | `test_a_paid_plan_header_on_an_error_response_trips_the_wire`, `test_a_429_from_a_paid_plan_trips_the_wire_before_any_retry` |
| Usage ledger and stops | A stop or a daily cap held only inside one `RECAP_DATA_DIR`; renaming the provider entry escaped a stop | One ledger per user (`~/.local/state/recap/llm-usage.db`, or `RECAP_USAGE_LEDGER`), keyed by host; the stop is re-checked inside the reservation's transaction | 4 tests in `tests/unit/test_m2_review.py` |
| Proxies | `HTTP_PROXY` (or a system proxy) could carry local prompts to a proxy, which could answer as "the model" | Model traffic ignores proxy settings | `test_proxy_settings_never_carry_a_local_request` |
| The record of requests | A retried 429 or 5xx, and the requests of a decision that then failed, were missing from the trace and from `model_requests` | Every HTTP request is its own `llm.call`; a failed decision's requests travel with the error and are recorded | 3 tests in `tests/unit/test_m2_review.py` (invariant I23) |
| Labels | Configuration could pair a hosted API with the "local" origin; a red-team run hid a fired tripwire and printed $0 | The origin is fixed by the class and hosted URLs need `https://`; the red-team wrapper forwards everything and shows a RED-TEAM chip; a fired tripwire makes any cost line "cost unknown" | `test_the_origin_is_fixed_by_the_code_and_hosted_means_https`, `test_the_red_team_wrapper_hides_neither_the_tripwire_nor_itself` |
| Model text | Terminal escape codes reached the console; corrective feedback quoted the model's own text back to it | Control characters stripped; feedback never quotes the model | `test_terminal_escape_codes_are_dropped_from_model_text`, `test_feedback_to_the_model_never_quotes_the_model` |
| Recordings and replay | A log line with `token="..."` broke a recording; a recorded short 429 deferred the replay; a replayed failure counted as usage; a mismatch in one parallel agent let the other finish the run | Redacted as data, then serialized; replays reuse the recorded settings and budget, count no usage and never sleep; `ReplayMismatch` and `FreeStatusLost` stop the whole run; a quota stop wins over a budget stop | 6 tests in `tests/unit/test_m2_review.py` |
| Protocol (now prompts-v4.1) | Tool definitions changed every step; a `tool_name` key could redirect a native tool call; a shared argument claimed one tool's range; a 5,000-digit number crashed parsing | Stable tool definitions; the called function decides; merged argument ranges; oversized numbers and Groq's `tool_use_failed` are unusable answers; argument names that collide with protocol fields are refused at startup | 5 tests in `tests/unit/test_m2_review.py`, `test_tool_definitions_stay_the_same_from_step_to_step` |
| Ollama | `...-CLOUD` in other letter cases; a local name that `/api/show` reports as served from a remote host | Both refused | 2 tests in `tests/unit/test_m2_review.py` |
| Smaller ones | A key pasted with curly quotes; the owner's confirmation compared with the UTC date instead of the local one; `GET /providers` probing any configured Ollama address (and calling it ready), or failing on an odd answer | The key is refused without being echoed; the local date; only a loopback Ollama is probed, and an odd answer is reported, not a crash | 4 tests in `tests/unit/test_m2_review.py` |
| Test quality | 19 places where a fix could be undone with no test failing (or failing for the wrong reason): look-alike loopback hosts, the strict-schema conversion, the hosted request's allowlists, an opt-in withdrawn or a stop recorded after startup, a retry's reservation, the wait before a 5xx retry, a key the redactor cannot recognize, the recording trims, a duplicate call, a proposal's extra arguments, the prompt-fit boundary, and more | A test for each | `tests/unit/test_m2_test_review.py` (29 tests) |

The replay fixture was re-recorded with prompts-v4.1 (local 3 above).

Test results after the review:

| Check | Command | Result |
|---|---|---|
| Python tests | `.venv/bin/python -m pytest` | **1150 passed, 1 skipped** in about 72 s (the skip is the optional OpenTelemetry test) |
| By folder | `--collect-only tests/<folder>` | unit 1000 · integration 27 · adversarial 62 · e2e folder 62 · total 1,151 |
| End-to-end | `.venv/bin/python -m pytest -m e2e` | **57** cases (53 functions) |
| Lint and format | `ruff check .` and `ruff format --check .` | clean |
| Dashboard tests | `cd apps/dashboard && npm test` | 19 passed, 0 failed |
| Dashboard build (strict TypeScript) | `npm run build` | passed |
| Smoke test | `.venv/bin/python scripts/smoke_test.py` | `RESOLVED (67 trace events, simulated environment)` (unchanged) |
| Evaluation (scripted) | `.venv/bin/python scripts/evaluate.py` | report `20261021-082547-5791a1` at git `85dd0f9`: every outcome, diagnosis, tool-call, step and token number identical to the M1-review report; only wall-clock latency and the fixture hash (it covers the re-recorded model run) differ |

### What genuinely ran

- **Real LLM inference:** `qwen3:8b` locally (33 requests in all: 25 in the three runs
  above and 8 direct measurement requests) and `gpt-oss-120b` on Groq's Free plan (16 chat requests, about
  39,500 reported tokens, plus 4 model-list metadata requests). **Charges: $0.**
- **Replays** of recorded real runs (no model called).
- **Simulated environment only.** Nothing was provisioned; no paid API was called.

### Still open (M2 blockers and follow-ups)

- **Human-approved deliverable run:** the M2 deliverable asks for a run that a human
  approves. The four runs above used the simulation auto-approver. The owner runs:
  `python scripts/demo.py --scenario bad-deployment --provider ollama --no-memory` and
  answers `y`; `scripts/export_run_trace.py <run_id> evals/live/<name>` then writes the
  sanitized trace.
- `gpt-oss-120b` tends to diagnose before reading the service's logs (twice in Groq 3).
  Telling models what evidence each mechanism needs, generically, is an M3 follow-up.
- Real models have not been red-teamed (prompt injection against `qwen3:8b`/`gpt-oss-120b`).
- Model-level runs are not reproducible (random IDs in the prompt); only replays are.
- **M5:** Docker, Kind and kubectl are still not installed.
