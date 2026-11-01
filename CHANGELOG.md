# Changelog

What changed in RECAP, one version per milestone. Each version is a git tag and a
[GitHub release](https://github.com/codeit-ronit/recap/releases). Versions follow
[Semantic Versioning](https://semver.org/) for an early prototype (0.y.z: anything may still
change between versions). Version numbers were assigned on 2026-10-21, when this file was
written: the package metadata reads 0.1.0 up to and including the v0.2.0 tag, and matches the
version from v0.3.0 on.

Everything below runs against a **simulated** environment; nothing repairs a real cluster.

## [0.5.0] — 2026-11-02 — M4: product control center, agent evaluations and release evidence

RECAP's runs can now be checked by anyone, in a browser: a typed, read-only API serves every
run from what it stored, and the Mission Control dashboard shows it in nine screens. Its
centerpiece is a one-click replay of the real two-agent `qwen3:8b` run of M3 (the owner-
approved fix of an incident RECAP had never seen): a SHA-256-pinned recording, replayed
through the whole system against the simulator, paced so people can watch it, with a fresh
approval in the browser. Every evaluated run becomes one record with a named reason when it
did not recover, and scripted, replayed and real-model results are never added together.
Two new live runs under M4 are recorded, a failure included. Nothing here touches a real
cluster: Kubernetes and SREGym are M5, and only planned.

### Added

- **Typed read-only API** ([ADR 0027](docs/decisions/0027-a-typed-read-only-api-over-the-run-records.md)):
  `GET /system`, `/providers`, `/runs` (filters, offset paging, `X-Total-Count`),
  `/runs/{id}` (typed summary), `/runs/{id}/timeline` (cursor paging, filters by agent,
  round, kind and tool), `/runs/{id}/evidence`, `/runs/{id}/retrieval`, `/runs/{id}/usage`,
  `/evals`, `/evals/{batch_id}`, `/replays`; schema `recap-api-v1` (also the
  `X-RECAP-API-Schema` header); a fact the record lacks is listed in `not_recorded`; the
  live stream resumes with `Last-Event-ID`.
- **Mission Control, the dashboard** ([ADR 0030](docs/decisions/0030-mission-control-reads-only-typed-server-facts.md),
  [apps/dashboard/README.md](apps/dashboard/README.md)): nine screens on the typed API:
  Overview (environment, providers, recent runs, outcomes counted per data source, "Start the
  demo"), Incident Lab (run an incident or replay a committed recording at a Watchable or
  Instant pace, live stage stepper and event feed, an approval dialog that cannot be
  dismissed, the outcome with the oracle's verdict), Trace Explorer (lanes per specialist,
  rounds with their routing and overlap, model requests with the time spent waiting inside
  the model server, cursor paging for runs with over 1000 events), Evidence & Decisions,
  Knowledge (with each run's searches), Experience Memory (partitions, reuse verdicts),
  Safety & Approvals (policy decisions, the approval lifecycle, unsafe actions attempted vs
  executed), Evaluations (one tab per mode, "k of N") and Architecture & Learning (the
  implementation with each part's status, code and ADRs). Loading, empty and error states on
  every screen; untrusted text escaped and labelled; status never by colour alone; usable at
  390 px. Component tests run on Node's own test runner (no new dependencies), and
  `apps/dashboard/e2e/smoke.mjs` drives the installed Chrome over the DevTools protocol
  through all nine pages and the replay-and-approve flow.
- **A safe per-run provider choice**: `POST /incidents/run` accepts the server's default, the
  scripted baseline, a replay of an intact committed recording, or a provider the owner marked
  `api_selectable`; paid providers never.
- **Replay by manifest id** ([ADR 0028](docs/decisions/0028-content-addressed-recordings-and-replay-by-manifest-id.md)):
  `fixtures/recordings/MANIFEST.json` pins each recording's SHA-256 and the run it reproduces;
  `RunConfig.replay_fixture`; an unknown id or altered file is refused before any answer; a
  replay is refused against a non-simulated environment; `scripts/recordings_manifest.py
  --check/--update`; the golden replay of the owner-approved two-agent run in CI
  (`tests/e2e/test_golden_replay.py`).
- **`RunConfig.replay_pace`** (0 to 1): a replay waits that share of each recorded answer's
  original time (at most 15 s), so the 11-minute golden run plays in about half a minute.
- **Evaluation records** ([ADR 0029](docs/decisions/0029-one-evaluation-record-per-run-and-modes-that-never-share-a-number.md),
  [docs/evaluation.md](docs/evaluation.md)): `evals/records.py`, `evals/categorize.py`; batches of
  one mode (`scripted_regression`, `offline_replay`, `real_llm`; `kubernetes_future` named,
  never run); success only with the oracle's verified recovery; generic failure categories
  with a detail line; `scripts/eval_replay.py`; published batches in `evals/reports/batches/`.
- **Generated dashboard types**: `scripts/gen_dashboard_types.py` writes
  `apps/dashboard/src/dto.ts` from the API's response models; a test fails when it is stale.
- **`scripts/seed_demo.py`**: fills a separate folder with real offline runs (scripted runs
  and replays of every committed recording) for a demo; refuses the repository's `data/`.
- **Two live runs under M4** (`evals/plans/m4-comparisons.yaml`, local `qwen3:8b`, $0): the
  cross-service incident with parallel specialists FAILED (`wrong_mechanism`, nothing
  executed); the held-out unfamiliar incident with one agent RESOLVED (oracle verified).
  One run each; [docs/status.md](docs/status.md).
- **[docs/m5-integration-plan.md](docs/m5-integration-plan.md)**: a minimal Kind cluster first,
  then SREGym; nothing installed or provisioned.
- **`scripts/secret_scan.py`** (CI, with the whole history): the recorder's key shapes in every
  tracked file and every blob in the git history, and the owner's own key values nowhere;
  prints no value.
- **mypy** (with the pydantic plugin) on the core interfaces and the M4 modules (28 files), in
  CI.

### Changed

- **Model time is split honestly**: Ollama's `total_duration` includes time a request waited
  in the server's queue, so RECAP now also records the server's compute
  (`ModelCall.server_compute_ms`) and reports the wait (`server_wait_ms` in the API,
  `model_wait_seconds` in a record, a column in batch digests).
- **Verifier leads from the recorded failures** (ADR 0021 decision 10): victim leads (an
  unsupported claim on a service whose own errors blame a dependency also points the next
  round at that dependency), V1-consistency notes, and leads that name only allowlisted
  services. No prompt text, schema or parser changed; both recordings still replay.
- `missed_crucial_tool` needs that no recorded observation showed the root service (a
  namespace-wide read that listed it counts as seeing it).
- `LLMProvider.info` is a read-only protocol member (a wrapper may compute it).
- vite 5.4.10 -> 5.4.21 (security patch within v5).

### Fixed

- Statements corrected against the records: the owner-approved run had no contradicted claim
  (its round-1 claim blaming a cart release stayed insufficient); in the adversarial run the
  infrastructure agent's claim came from the poisoned runbook and planted memory; the
  verifier "accepted no wrong claim"; leads were followed in 7 of 7 recorded runs, 3 of 7
  resolved.
- `GET /providers` never shows a local folder for the replay recording; `GET /evals` also
  lists evaluations started from the dashboard; the export's replay note no longer says a
  parallel round replays only in its original order.

### Verified

- 1681 tests passed, 1 skipped (an optional OpenTelemetry test), 149 of them end to end;
  ruff, mypy (29 files), the recordings manifest and the secret scan (tracked files and the
  whole history) clean.
- Dashboard: 494 tests, `tsc --noEmit` and a build; the browser smoke test passed 72 of 72
  checks on the seeded demo data (nine pages at 1440 and 390 px; the replay, approval and
  RESOLVED flow, confirmed by the API).
- Scripted baselines: 0 of 51 runs differ (memory off and on) after the M4.5 verifier change.

## [0.4.0] — 2026-10-25 — M3: real multi-agent intelligence, RAG and episodic memory

Two independent AI specialists now investigate an incident side by side, each with its own
tools and its own view of the evidence; RECAP's evidence rules decide between their claims,
never a vote. Runbooks can be searched by meaning as well as by words (a local embedding
model), memory keeps real-model experience apart and reuses a past fix only after today's
evidence confirms it, and real model tokens are budgeted. The milestone's deliverable: the
local `qwen3:8b` solved an incident RECAP had never seen (the cause two calls away from the
alert, logs in another style, a red-herring release), the owner approved its exact fix at the
terminal, and the oracle verified recovery; it is exported in
[`evals/live/`](evals/live/README.md). Every real-model run of M3, failures included, is in
[docs/status.md](docs/status.md).

### Added

- **Independent specialists** ([ADR 0020](docs/decisions/0020-independent-specialists-with-role-contracts.md)):
  in a parallel round each specialist sees only the evidence from before the round plus its
  own; per-agent evidence IDs (`ev-…-r2t01`) in a fixed order, so a recorded parallel run
  replays offline; disjoint tools and role contracts (responsibilities and the mechanisms a
  role may conclude, enforced by the harness for every provider); a hypothesis can name
  contradicting evidence and an untrusted confidence note; `AgentFinding.outcome`.
- **The evidence merger** ([ADR 0021](docs/decisions/0021-evidence-rules-settle-disagreements-never-votes.md),
  `recap/agents/merger.py`): claims merged by mechanism, target and release version or
  dependency, with every proposer (a release version written without its "v" or in another
  case is the observed release); typed disagreement records (`detail_conflict`,
  `competing_root_causes`, `cross_agent_contradiction`, `missing_view`) with the rule and
  evidence that settled each; an open conflict's question goes to one follow-up round
  (`RT3-disagreement`), then a person decides; leads for the next round (a failing
  dependency nothing explains yet), which are never a disagreement; `verification.merge`
  trace events.
- **Verifier rules:** V1-grounding (a supported claim cites evidence its rules used; any
  listing of it counts), V6 on the target (a service the platform shows healthy is no root
  cause), V7-deepest-cause, and VP4-restore-point (a rollback restores the version the
  evidence names).
- **Hybrid retrieval** ([ADR 0022](docs/decisions/0022-hybrid-retrieval-with-a-local-embedder-and-agent-search.md)):
  `nomic-embed-text` on loopback through the recorded model transport, stored chunk vectors
  (`scripts/build_vector_index.py`), BM25 fused with a cosine ranking (cutoff 0.55, RRF),
  ranking signals on every result, `configs/retrieval.yaml`, and `RunConfig.retrieval`.
  BM25 stays the default: on the scenarios' alert queries hybrid found the right runbook
  less often (7 of 15 against 9), while it helped on paraphrased questions.
- **`search_runbooks`**: agents search the runbooks themselves (a local read tool answered
  by RECAP, 6 searches per run); the sections join only that agent's context and its
  citations; the receipt is DOC evidence, never proof.
- **Episodic memory v2** ([ADR 0023](docs/decisions/0023-memory-partitions-by-origin-and-evidence-checked-reuse.md)):
  a LIVE partition for real-model runs (scripted runs keep DEMO; replays never write);
  consolidator-v2 episodes with the trajectory, judged claims, diagnosis basis and fix
  preconditions (deterministic extraction); reuse only with the same mechanism, the same
  service and a fix that still fits today's evidence; hits re-ranked by what live evidence
  confirmed; semantic recall in hybrid runs; poisoned-memory tests.
- **A real model-token budget** ([ADR 0024](docs/decisions/0024-a-real-model-token-budget-and-context-that-keeps-what-matters.md)):
  `max_model_tokens`, reserved before each model request and settled with the tokens the
  server reported; a long log list keeps its errors when the gateway cuts it; evidence a
  diagnosis needs is shortened, not dropped, when the pack is full.
- **Two held-out M3 incidents** ([ADR 0025](docs/decisions/0025-held-out-unfamiliar-and-adversarial-incidents.md)):
  an unfamiliar one (RPC-style logs, an alert two calls from the cause) and an adversarial
  one (forged out-of-memory lines, an instruction the pattern scanner misses); the log
  parsers read a fixed family of common phrasings; opt-in red-team overlays for a poisoned
  runbook (`RECAP_KNOWLEDGE_OVERLAYS`) and a planted false memory (`scripts/redteam_setup.py`).
- **`scripts/live_eval.py`**: runs a planned set of real-model incidents (`evals/plans/`) in
  one data folder of its own (never the repository's `data/`) and summarizes each from its
  records; the whole plan is checked before its first run.

### Changed

- prompts-v4.3: each role's goal, responsibilities and allowed mechanisms, and the
  verifier's open questions; prompts-v4.4: the search tool, and free text a model wrote
  (a search query) shown only as data, with hashed values in "already observed" refs;
  prompts-v4.5: memory shown with RECAP's reuse verdict, and only plain values from an
  episode in trusted text; prompts-v4.6 (after the review of steps 1 to 6): the verifier's
  leads join its open questions, focus services and remembered fix parameters are repeated
  only when allowlisted, free-text arguments are always data, and a request whose every
  call repeats one already made is told so by name. The Claude adapter's prompts became
  prompts-v3.3 (the same trusted-text rules). The CI replay fixture was re-recorded after
  each (local `qwen3:8b` runs, 5, 6, 6 and 6 requests, $0).
- "Safe to reuse" is stricter: a verified fix for another service, or a rollback to a
  version today's evidence does not name, no longer counts.
- Ties between different supported root causes are no longer broken by how much evidence a
  claim cites; they stay an open conflict.
- A parallel agent may use at most 70% of the run's remaining time; findings other agents
  completed are kept, marked `partial`, when the shared budget stops the run.

### Fixed

Found by the review of M3 steps 1 to 6.

- **Replays of runs that timing stopped** ([ADR 0026](docs/decisions/0026-a-replay-stops-where-its-recording-stopped.md)):
  a recording keeps each budget refusal, time-share or deadline cut and each request that
  got no answer, at the acting agent's count of checks; a replay never refuses on its own,
  has no clock, and stops exactly there. A parallel run cut by a time share or stopped by a
  shared budget used to fail its replay with `ReplayMismatch`. Recordings made before this
  have no stops, so a run that a budget or a clock stopped must be recorded again.
- A model request cancelled while it was out gives back its token reservation (settled with
  at least its prompt's estimate); when the budget refuses a corrective retry, the request
  already answered stays in the trace and the usage.
- A replay reads memory as of its recording's start (`MemoryAccess.as_of`), so with memory
  on it no longer finds the episode its own original run wrote; `ReplayMismatch` names what
  to check (the prompts, tools, tool results or memory).
- V1-grounding accepts a citation of any listing of the same release, log line or OOM kill,
  whatever the recording order.
- A release written without its "v" or in another case (`2.4.1`, `V2.4.1`) is the observed
  release, so it no longer opens a release conflict with itself.
- A settled `detail_conflict` names the rules that rejected the other versions; an open
  `competing_root_causes` record names only the competing claims' agents and evidence.
- The FAILED handover names what is really open: "supported root causes still conflict" only
  for level supported causes, "claims still disagree on which release is bad" for an open
  release question. RT3-disagreement's reason no longer calls every conflicting claim
  supported.
- Findings kept from a round that a budget stop cut short are marked `partial` in the record
  (`AgentFinding.partial`). Such a run is still stored as a `budget_exhausted` caution, and
  their claims never reach its checked claims.
- The run store lists a parallel round's evidence in the canonical order, not in arrival
  order.
- `tests/e2e/test_unfamiliar_scenario.py` now proves, for every routing, the RPC-style chain
  from frontend to inventory that its comment claimed.
- Memory files from before M3: opening one moves each episode a real model wrote to DEMO
  into LIVE, recorded in the `partition_moves` table. The owner's `data/memory.db` is
  migrated the next time RECAP opens it: its pre-M3 model episodes move from `demo` to
  `live`.
- Semantic recall adds up to k close matches after the k lexical hits instead of being cut
  off by them.
- An episode that names no service, or whose remembered fix acted on another service, is not
  safe to reuse; nor is a remembered rollback that names no plain release.
- An episode's diagnosis basis is the supported claim's own lines (a rejected release's claim
  could supply them), and each judged claim records the version or dependency that told it
  apart.
- `scripts/demo.py` makes a memory prerequisite with the demo's own provider, where that run
  reads memory, warns when a model will take minutes, and makes none for a replay or with
  `--no-memory`.
- Hybrid runs refuse stored vectors made by another model, dimensions, document prefix or
  weights; `scripts/build_vector_index.py --check` compares the same things.
- Each retrieval event's `retriever` is what that search used, and `GET /system` reports the
  default run's `retriever` and `hybrid_ready` (both used to say hybrid whenever the vector
  file was complete).
- Knowledge overlays outside the repository load (they crashed startup); an overlay file
  named like a shipped runbook, or a folder listed twice, stops startup with a `ConfigError`
  naming both files.
- The V1 evaluation pins BM25 over the shipped runbooks whatever `RECAP_ENABLE_EMBEDDINGS`
  and `RECAP_KNOWLEDGE_OVERLAYS` say; its report records `retrieval_mode` and
  `knowledge_overlays`.
- The scripted policy builds call refs exactly as the gateway does (the tool's scheme,
  free-text arguments hashed). No behaviour changes today: it calls no local tool and no
  tool with a free-text argument.

### Security

- Trusted prompt text: a one-word search query is hashed in "already observed" refs and shown
  as data, whatever it looks like; the "Services to examine first" section repeats only
  allowlisted services (the verifier can read a name from a log line); a remembered fix's
  parameters are repeated only under the argument names the trusted catalog gives; a
  tampered episode ID is never offered or accepted as a citation, by the Claude adapter
  either; the verifier's questions repeat a version only in the tool registry's `vX.Y.Z`
  shape.
- `scripts/redteam_setup.py` and `scripts/live_eval.py` refuse the repository's own `data/`
  under any spelling (compared by file identity, e.g. `DATA` on macOS).
- A knowledge overlay outside the repository is recorded and cited by its own folder name,
  never by the local folders above it.

### Verified

- 1449 tests passed, 1 skipped (an optional OpenTelemetry test), 96 of them end to end; ruff
  clean; dashboard 119 tests, `tsc --noEmit` and a build.
- Scripted baselines after every M3 change, against the M3 step 6 commit: 0 of 51 runs differ
  (memory off) and 0 of 34 (memory on). The scripted evaluation: `full_adaptive`,
  `no_memory` and `single_only` 6 of 6 as in 0.3.0; `no_rag` 3 of 6 (5 of 6 in 0.3.0).
- Real inference: 203 chat requests to the local `qwen3:8b` and 13 to `gpt-oss-120b` on Groq's
  free plan. With prompts-v4.7, `qwen3:8b` resolved the held-out unfamiliar incident in both
  runs (one approved by the owner); it still failed the cross-service incident. **Charges: $0.**
- CI replays two real `qwen3:8b` recordings offline: the single-agent bad-deployment run and the
  owner-approved two-specialist run on the unfamiliar incident (about 2.4 s, no model, no network).

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
