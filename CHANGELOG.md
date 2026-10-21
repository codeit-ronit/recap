# Changelog

What changed in RECAP, one version per milestone. Each version is a git tag and a
[GitHub release](https://github.com/codeit-ronit/recap/releases). Versions follow
[Semantic Versioning](https://semver.org/) for an early prototype (0.y.z: anything may still
change between versions). Version numbers were assigned on 2026-10-21, when this file was
written: the package metadata reads 0.1.0 up to and including the v0.2.0 tag, and matches the
version from v0.3.0 on.

Everything below runs against a **simulated** environment; nothing repairs a real cluster.

## [Unreleased] — M3 (in progress): real multi-agent intelligence, RAG and episodic memory

### Added

- **Independent specialists** ([ADR 0020](docs/decisions/0020-independent-specialists-with-role-contracts.md)):
  in a parallel round each specialist sees only the evidence from before the round plus its
  own; per-agent evidence IDs (`ev-…-r2t01`) in a fixed order, so a recorded parallel run
  replays offline; disjoint tools and role contracts (responsibilities and the mechanisms a
  role may conclude, enforced by the harness for every provider); a hypothesis can name
  contradicting evidence and an untrusted confidence note; `AgentFinding.outcome`.
- **The evidence merger** ([ADR 0021](docs/decisions/0021-evidence-rules-settle-disagreements-never-votes.md),
  `recap/agents/merger.py`): claims merged by mechanism, target and release version or
  dependency, with every proposer; typed disagreement records (`detail_conflict`,
  `competing_root_causes`, `cross_agent_contradiction`, `missing_view`) with the rule and
  evidence that settled each; an open conflict's question goes to one follow-up round
  (`RT3-disagreement`), then a person decides; `verification.merge` trace events.
- **Verifier rules:** V1-grounding (a supported claim cites evidence its rules used), V6 on
  the target (a service the platform shows healthy is no root cause), V7-deepest-cause, and
  VP4-restore-point (a rollback restores the version the evidence names).
- **Hybrid retrieval** ([ADR 0022](docs/decisions/0022-hybrid-retrieval-with-a-local-embedder-and-agent-search.md)):
  `nomic-embed-text` on loopback through the recorded model transport, stored chunk vectors
  (`scripts/build_vector_index.py`), BM25 fused with a cosine ranking (cutoff 0.55, RRF),
  ranking signals on every result, `configs/retrieval.yaml`, and `RunConfig.retrieval`.
  BM25 stays the default: on the scenarios' alert queries hybrid found the right runbook
  less often (7 of 15 against 9), while it helped on paraphrased questions.
- **`search_runbooks`**: agents search the runbooks themselves (a local read tool answered
  by RECAP, 6 searches per run); the sections join only that agent's context and its
  citations; the receipt is DOC evidence, never proof.

### Changed

- prompts-v4.3: each role's goal, responsibilities and allowed mechanisms, and the
  verifier's open questions; prompts-v4.4: the search tool, and free text a model wrote
  (a search query) shown only as data, with hashed values in "already observed" refs. The
  CI replay fixture was re-recorded after each (local `qwen3:8b` runs, 5 and 6 requests, $0).
- Ties between different supported root causes are no longer broken by how much evidence a
  claim cites; they stay an open conflict.
- A parallel agent may use at most 70% of the run's remaining time; findings other agents
  completed are kept when the shared budget stops the run.

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
