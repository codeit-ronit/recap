# Changelog

What changed in RECAP, one version per milestone. Each version is a git tag and a
[GitHub release](https://github.com/codeit-ronit/recap/releases). Versions follow
[Semantic Versioning](https://semver.org/) for an early prototype (0.y.z: anything may still
change between versions). Version numbers were assigned on 2026-10-21, when this file was
written: the package metadata reads 0.1.0 up to and including the v0.2.0 tag, and matches the
version from v0.3.0 on.

Everything below runs against a **simulated** environment; nothing repairs a real cluster.

## [0.3.0] — 2026-10-21 — M2: real models at $0

A real language model can now make RECAP's decisions without spending anything:
`qwen3:8b` on the owner's laptop through Ollama, or `gpt-oss-120b` on Groq's free plan behind
a fail-closed guard. The scripted policy stays the default. The milestone's deliverable, a
real-model incident that the owner approved at the terminal and the oracle verified, is
exported for anyone to check in [`evals/live/`](evals/live/README.md).

### Added

- **Model providers** behind the unchanged `LLMProvider` interface, over plain HTTP:
  Ollama (loopback only, native tool calls, exact token counts) and Groq's free plan
  (strict JSON schemas). [ADR 0016](docs/decisions/0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md)
- **A step protocol** (`recap/providers/protocol.py`, now prompts-v4.2): every request's JSON
  schema is built from RECAP's trusted catalog, so a model can only name listed tools,
  allowlisted services and evidence IDs it has seen; an unusable answer gets one corrective
  retry and never a scripted stand-in.
- **A free-tier guard** (`recap/providers/quota.py`): an explicit opt-in, an exact model
  allowlist, the owner's dated free-plan confirmation, caps below the free limits in a
  per-user ledger, and a tripwire that compares every response's rate-limit header with the
  free plan's. A run whose free quota runs out ends `QUOTA_EXHAUSTED` (deferred), never on
  another provider. [ADR 0017](docs/decisions/0017-fail-closed-free-tier-guard-and-a-deferred-state.md)
- **Recording and replay** (`recap/providers/transport.py`): every model exchange is recorded
  without keys or account IDs; a `replay` provider re-runs a recorded run offline, and CI
  replays a real `qwen3:8b` incident. [ADR 0018](docs/decisions/0018-record-every-model-exchange-and-replay-in-ci.md)
- **Checkable evidence of a run** ([ADR 0019](docs/decisions/0019-checkable-evidence-of-a-real-model-run.md)):
  every run records its code version and the model server's facts (Ollama version,
  quantization, weights digest); console approvals are read only from an interactive
  terminal and log how the answer came; `scripts/export_run_trace.py` writes a complete,
  sanitized, replayable export and refuses to write anything that still holds a secret,
  an account ID, a local path or the user's login name.
- **Dashboard and API:** `GET /providers`, run-origin badges (SCRIPTED, LOCAL LLM, FREE
  HOSTED LLM, REPLAY), model-call lines in the live event feed, a providers panel.
- **Scripts:** `scripts/demo.py --provider`, `scripts/providers.py` (readiness, usage,
  clear-stop), `scripts/export_run_trace.py`.
- **Tests:** local protocol fixtures (fake Ollama and Groq servers), a replayed real run,
  a guard that no scenario's answer appears in any fixed prompt text, the export's safety
  gates, and a scan of every published file.

### Changed

- The paid Claude adapter now needs `RECAP_ALLOW_PAID_LLM=1` on top of an API key; it stays
  off in this $0 project.
- Fixed prompt text carries no example values (prompts-v4.2 for the free and local models,
  prompts-v3.2 for the paid adapter): a review found that prompts-v4.1's examples were
  exactly the test scenario's answer.

### Security

Three review rounds, each fix checked by undoing it and watching its test fail (51 checks for
the code review, 39 for the reviews of the human-approved run; all caught):

- the tripwire reads every response, errors included; stops and caps hold per user and per
  provider host; model traffic ignores proxy settings;
- every HTTP request is recorded, including retried and failed ones;
- recordings and exports keep no provider request or answer IDs;
- a typed-ahead "y" no longer counts as an approval, and very fast answers are flagged.

### Verified

- 1217 tests passed, 1 skipped (an optional OpenTelemetry test); ruff clean; dashboard 19/19
  and a strict build; the scripted evaluation's numbers are unchanged.
- Real inference: `qwen3:8b`, 6 incidents (42 requests, 111,146 / 5,437 tokens);
  `gpt-oss-120b` on Groq's free plan, 3 incidents (16 requests, 35,816 / 3,738 tokens).
  **Charges: $0.**
- Measured results, review findings and open items:
  [docs/status.md](docs/status.md#m2-zero-cost-real-model-providers-2026-10-21).

## [0.2.0] — 2026-10-15 — M0 and M1: audit reproduction and hardening

An independent audit of V1 found that its safety and evidence guarantees could be broken.
Every finding was first reproduced as a failing test (M0), then fixed (M1); three more
reviews then hardened the fixes.

### Added

- **M0:** pinned dependency versions (`constraints.txt`), the baseline measured, and the
  audit's findings reproduced as 43 strict-xfail tests that fail on the V1 code.
  [ADR 0009](docs/decisions/0009-audit-findings-as-strict-xfail-tests.md)
- An `e2e` pytest marker that counts end-to-end tests separately.

### Changed (the audit fixes)

- **Context:** providers read structured evidence summaries (newest errors, counts, metric
  extremes) instead of raw records that the packer cut from the head.
  [ADR 0011](docs/decisions/0011-structured-evidence-summaries.md)
- **Evidence:** platform signals outrank log text, so forged log lines alone prove nothing.
  [ADR 0012](docs/decisions/0012-platform-signals-outrank-log-text.md)
- **Oracle isolation:** providers see label-free views; a retry's focus comes from RECAP's
  own reasoning, never from the oracle. [ADR 0010](docs/decisions/0010-label-free-views-and-a-yes-no-oracle.md)
- **Recovery:** stabilization waits for live targets, epoch-aware verification, provider
  calls bound by the run's deadline. [ADR 0013](docs/decisions/0013-stabilization-epochs-and-deadlines.md)
- **Execution authority:** every write carries an Ed25519-signed, single-use capability that
  the environment verifies. [ADR 0014](docs/decisions/0014-signed-capabilities-at-the-write-boundary.md)
- **Approvals and API:** "no" wins until an approval is used; compare-and-swap transitions;
  an append-only decision log; an operator-authenticated API with host and origin checks.
  [ADR 0015](docs/decisions/0015-operator-auth-and-no-wins-approvals.md)

### Verified

- 941 tests passed, 1 skipped; 25 review fixes checked by undoing them.
- Evaluation report `20261015-132617-a08fdf`: outcomes and diagnoses as in V1; context
  tokens 23–54% lower.
- Details: [docs/status.md](docs/status.md#m1-security-context-and-oracle-hardening-2026-10-15).

## [0.1.0] — 2026-10-15 — V1 prototype

The first public version: an incident-response agent that investigates through real MCP
tools, asks a human to approve one exact fix, checks recovery with an independent oracle and
remembers only verified experience.

- Typed contracts and configuration; a replaceable environment adapter with simulated
  scenarios and a recovery oracle.
- An MCP tool server and client, a safety gateway with a policy, and approvals.
- Runbook retrieval (BM25), episodic memory, context packing and trace sinks.
- Decision providers (a scripted policy by default; an optional paid Claude adapter), an
  evidence verifier, adaptive routing and the incident controller.
- A FastAPI backend and a React dashboard; an evaluation harness with a train/test split;
  documentation and CI.
- 781 tests passed, 1 skipped (measured at the start of M0).

[0.3.0]: https://github.com/codeit-ronit/recap/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/codeit-ronit/recap/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/codeit-ronit/recap/releases/tag/v0.1.0
