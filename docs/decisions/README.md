# Architecture decision records

An **architecture decision record (ADR)** is a short note about one important design choice:
the situation that forced it (*context*), what was chosen (*decision*), what it costs and
what it gains (*consequences*), and what else was considered (*alternatives*). Together,
these notes explain why RECAP looks the way it does.

| ADR | Decision | Status | In one line |
|---|---|---|---|
| [0001](0001-modular-monolith.md) | Build V1 as a modular monolith | accepted | One Python package with typed contracts, interfaces and one composition root; the MCP tool server is the only extra process. |
| [0002](0002-sqlite-storage.md) | Store everything in local SQLite files | accepted (amended by 0015) | Runs, traces, approvals, memory and the simulated world live under `data/`; single-use approvals are atomic SQL updates. |
| [0003](0003-deterministic-default-provider.md) | A scripted, deterministic policy is the default provider | accepted | The default "brain" is not an LLM, costs $0 and is reproducible; the Claude adapter is opt-in with your own key. |
| [0004](0004-real-mcp-over-stdio.md) | Reach the environment through a real MCP server over stdio | accepted (amended by 0014) | Ten real MCP tools in a subprocess per run; the server is treated as untrusted (since M1 its writes need a signed capability). |
| [0005](0005-central-safety-gateway-and-executor.md) | One central safety gateway, and the controller as the only executor | accepted (extended by 0014, 0015) | One gateway judges every call from trusted config; reversible fixes need an exact single-use approval; destructive actions are always denied. |
| [0006](0006-simulated-environment-for-v1.md) | Use a deterministic simulated environment for V1 | accepted | A five-service shop that runs offline in seconds, can be broken safely, and is labeled "simulated" everywhere. |
| [0007](0007-replaceable-environment-adapter.md) | Make the target environment a replaceable adapter | accepted | Infrastructure sits behind `TargetEnvironment`; SREGym or Kubernetes can replace the simulator, and a test proves the swap's wiring. |
| [0008](0008-pinned-versions-with-a-constraints-file.md) | Pin exact package versions with a constraints file | accepted | `constraints.txt` fixes every Python package to the version the suite last passed with; CI and bootstrap install with `-c`. |
| [0009](0009-audit-findings-as-strict-xfail-tests.md) | Reproduce every audit finding as a strict xfail test before fixing it | accepted | 43 reproductions and 11 guards; a fix turns its test green and removes the mark in the same change. |
| [0010](0010-label-free-views-and-a-yes-no-oracle.md) | Providers get label-free views, and the oracle only ever says yes or no | accepted | `AgentIncident`/`AgentMemoryHit` have no label fields; after a failed fix the focus comes from RECAP's own reasoning (audit 2.3). |
| [0011](0011-structured-evidence-summaries.md) | Providers read structured evidence summaries; no cut keeps only the head | accepted | Newest errors, counts and metric extremes survive any pack; the diagnosis' runbook is pinned (audit 2.1). |
| [0012](0012-platform-signals-outrank-log-text.md) | Platform signals outrank log text in the evidence verifier | accepted | Rule V6: OOM text alone proves nothing, and blame toward a healthy dependency does not count (audit 2.2). |
| [0013](0013-stabilization-epochs-and-deadlines.md) | Wait for live targets to settle, judge each epoch on its own evidence, bound every provider call | accepted | Bounded stabilization polling, pre-fix state of a fixed service ignored, BUDGET_EXHAUSTED at the deadline (audit 2.6). |
| [0014](0014-signed-capabilities-at-the-write-boundary.md) | The write boundary verifies signed, single-use capabilities | accepted | Ed25519 per run; the environment refuses any write the gateway did not sign for exactly that action (audit 2.5). |
| [0015](0015-operator-auth-and-no-wins-approvals.md) | An operator-authenticated API, and approvals where "no" wins until the approval is used | accepted | Bearer token, identity from the credential, Host and Origin checks, compare-and-swap transitions, append-only log (audit 2.4). |
| [0016](0016-zero-cost-llm-providers-and-a-constrained-step-protocol.md) | Real model inference only from local or verified-free providers, through one constrained step protocol | accepted | Ollama (local) and Groq Free over plain HTTP; per-request schemas from the trusted catalog; one corrective retry, never a scripted stand-in (M2). |
| [0017](0017-fail-closed-free-tier-guard-and-a-deferred-state.md) | A fail-closed free-tier guard, and a deferred state when a free quota runs out | accepted | Opt-in, allowlist, dated owner confirmation, caps below the free limits, a rate-limit-header tripwire; `QUOTA_EXHAUSTED` instead of another provider (M2). |
| [0018](0018-record-every-model-exchange-and-replay-in-ci.md) | Record every model exchange, and replay real model runs offline in CI | accepted | HTTP-level recordings without keys; fingerprint matching; a `REPLAY`-labeled provider runs a real qwen3:8b run in CI at $0 (M2). |
| [0019](0019-checkable-evidence-of-a-real-model-run.md) | A real-model run is evidence only if a reader can check it | accepted | No example value in fixed prompt text (guard test); console approvals need a terminal and log how they came; code version and model weights on every run; a complete export that refuses to write secrets or local traces (M2). |
| [0020](0020-independent-specialists-with-role-contracts.md) | Parallel specialists work independently, under role contracts, with order-free evidence IDs | accepted | Each specialist sees pre-round evidence plus its own; disjoint tools; it may conclude only what its tools can observe; per-agent IDs make a parallel run replayable (M3). |
| [0024](0024-a-real-model-token-budget-and-context-that-keeps-what-matters.md) | A real model-token budget, and context that keeps what matters under pressure | accepted | `max_model_tokens` reserved before each request and settled with the server's counts; long logs keep their errors when cut; must-keep evidence shortened, not dropped (M3). |
| [0023](0023-memory-partitions-by-origin-and-evidence-checked-reuse.md) | Memory keeps each kind of run apart, and reuse is decided by live evidence alone | accepted | DEMO for scripted runs, LIVE for real models, no writes from replays; consolidator-v2 trajectories; reuse needs the same mechanism, the same service and a fix that still fits; similarity never decides (measured) (M3). |
| [0022](0022-hybrid-retrieval-with-a-local-embedder-and-agent-search.md) | Hybrid retrieval with a local embedding model, and agents that search the runbooks themselves | accepted | nomic-embed-text on loopback, recorded and replayed; stored chunk vectors; BM25 stays the default (measured); a budgeted `search_runbooks` tool answered by RECAP; free text a model wrote stays data (M3). |
| [0021](0021-evidence-rules-settle-disagreements-never-votes.md) | Evidence rules settle the agents' disagreements, never votes | accepted | Claims merged by version and dependency; V1-grounding, V6 on the target, V7 without tie-breaks; typed disagreement records; an open conflict gets one follow-up question, then a person; VP4 restore points (M3). |

## How the decisions fit together

- **0001** sets the overall shape: modules behind interfaces, wired in one place.
- **0002, 0003 and 0006** keep V1 local, free and reproducible: SQLite files, a scripted
  provider and a simulated target system.
- **0004 and 0005** define the tool boundary and who may act: agents use MCP tools, every
  call passes the gateway, and only the controller executes a fix.
- **0007** keeps the target system swappable, so the work above carries over to real
  infrastructure.
- **0008 and 0009** are the rules of the next phase: reproducible installs, and no audit
  finding is fixed without a test that failed first.
- **0010 to 0015** are the M1 audit fixes. 0010 and 0011 decide what a provider may see and
  in what form; 0012 decides what evidence counts; 0013 shapes the recovery lifecycle; 0014
  and 0015 move enforcement to the write boundary and to an authenticated operator.
- **0016 to 0018** bring real models in at $0 (M2). 0016 decides which models and how they
  are asked; 0017 keeps a hosted free plan free or stops; 0018 records every exchange so a
  real model run can be replayed offline in CI. The scripted policy of 0003 stays the default.
  0019 came from reviewing the first human-approved run: it makes such a run checkable
  (neutral prompts, an attested approval, code and weights provenance, a guarded export).
- **0020 and 0021** make the two specialists real (M3). 0020 gives each an independent view
  and a role it cannot leave; 0021 decides how their claims combine: by evidence rules
  only, with every disagreement recorded, and a person deciding what the rules cannot.
- **0022** gives retrieval a meaning-based ranking from a local model and lets agents search
  themselves, without letting either the environment or a model's free text into trusted
  context.
- **0023** separates the memory of scripted, real-model and evaluation runs, and makes
  "safe to reuse" a question for live evidence, never for similarity.
- **0024** bounds what real models may spend by what they report, and makes the context
  keep errors and contradicting evidence when space runs out.

The [threat model](../threat-model.md) builds on 0004, 0005, 0007, 0014 and 0015. It lists
every threat RECAP defends against, where the defense lives in the code, and the test that
proves it. [flow.md](../flow.md) follows one incident through all of these decisions.

## Two kinds of status

- **ADR status** says whether a decision is current: `proposed`, `accepted`, `superseded by
  ADR NNNN` or `deprecated`. All ADRs above are `accepted`; "amended by" marks an
  accepted decision that a later ADR changed in part.
- **Feature status**, used across the documentation, says how real a part is:
  *implemented* (works in V1 and is tested), *simulated* (works only against the simulator,
  or stands in for something real), *adapter only* (an interface or optional bridge exists,
  nothing real is connected) and *not yet built* (planned only).

## Adding an ADR

1. Copy the template below into `NNNN-short-title.md`, using the next free number.
2. Keep it short and specific. Link the code it is about with relative links.
3. Never rewrite an accepted decision. When a decision changes, write a new ADR and mark the
   old one `superseded by ADR NNNN`.
4. Add a row to the table above.

```markdown
# ADR NNNN: <decision as a short sentence>

**In one sentence:** <the decision and its main reason>

## Status

Proposed.

## Context

<the situation and the forces that make a decision necessary>

## Decision

<what was chosen, with links to the code that implements it>

## Consequences

### Good

- <what gets easier or safer>

### Bad

- <what gets harder, riskier or more expensive>

## Alternatives considered

- **<option>.** <why it was not chosen>
```
