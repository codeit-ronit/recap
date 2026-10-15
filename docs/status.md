# RECAP status

This page records what was actually measured, milestone by milestone. Every number below
comes from a command that was run; nothing is projected. It is updated at the end of each
milestone of the zero-cost next phase (M0 to M6).

**Cost so far: $0.** No LLM API request of any kind has been sent in this phase.

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
6. **Approvals and API (2.4).** A rejection wins until the action runs; transitions are
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
| 2.4 Approvals and API | `2df849c` | "No" wins until the action runs; compare-and-swap transitions; append-only decision log; operator bearer token, host and origin checks; dashboard sign-in | [ADR 0015](decisions/0015-operator-auth-and-no-wins-approvals.md) |
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

### What genuinely ran

Scripted provider only (`deterministic-scripted-policy`, not an LLM), simulated environment
only. **No LLM request of any kind was sent, no model was downloaded, nothing was provisioned.
Cost: $0.**

### Still open (blockers for later milestones)

- **M2 (real LLM inference)** needs one owner decision: a verified-free hosted provider key
  (proposed: Groq Free plan, see [provider-options.md](provider-options.md)), and/or approval to
  download a local model (proposed: `qwen3:8b`, 5.2 GB; `llama3.2:3b` is already installed but
  is too small to diagnose reliably).
- **M5 (local Kubernetes)**: Docker, Kind and kubectl are not installed on this machine.
- The threat model lists what a real cluster still needs:
  [Before a real Kubernetes target](threat-model.md#before-a-real-kubernetes-target).
