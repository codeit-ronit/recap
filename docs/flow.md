# RECAP end-to-end flow: what goes in, what links to what, what comes out

This page follows one incident from the moment it starts to the moment it is remembered.
For every step it says **what comes in**, **which code handles it**, **what comes out**, and
**where that output goes next**. It describes the code as of milestone M1 (after the audit
fixes). The numbers in the example come from a real run of the `bad-deployment` scenario
(an operator approved the fix): 66 trace events, 8 tool calls, 9 provider decisions,
RESOLVED.

Companion pages: [architecture.md](architecture.md) (the components in depth),
[threat-model.md](threat-model.md) (who may do what), [status.md](status.md) (measured
results), and [decisions/](decisions/README.md) (why it is built this way).

---

## 1. The whole flow on one page

```mermaid
flowchart TD
    subgraph IN["Inputs"]
        OP["Operator: CLI, or dashboard + API with the operator token"]
        CFG["configs/*.yaml (trusted)"]
        SCN["fixtures/scenarios/*.yaml (the simulated fault)"]
        KB["fixtures/knowledge/*.md (runbooks)"]
    end

    OP -->|"scenario id + RunConfig"| RT["Runtime (recap/runtime.py)"]
    CFG --> RT
    RT --> CTL["IncidentController (recap/harness/controller.py)"]
    CTL -->|"reset(scenario, seed)"| ENV["TargetEnvironment adapter (simulator)"]
    SCN --> ENV
    ENV -->|"AlertSignal"| CTL
    CTL -->|"bind_executor(public key)"| ENV

    CTL -->|"RETRIEVING"| RAG["BM25 runbook search"]
    KB --> RAG
    CTL --> MEMS["Experience memory search"]

    CTL -->|"INVESTIGATING"| RTR["Router: single or parallel"]
    RTR --> ORC["Orchestrator"]
    ORC --> AG["Agent harness loop (one per agent)"]
    AG -->|"ContextPacker: summaries, label-free views"| PRV["Provider (scripted policy, or an LLM later)"]
    PRV -->|"read tool calls"| GW["ToolGateway"]
    GW -->|"MCP over stdio"| SRV["MCP server subprocess"]
    SRV -->|"observe_*"| ENV
    GW -->|"bounded, redacted Evidence"| REG["EvidenceRegistry"]
    AG -->|"AgentFinding (hypotheses citing evidence IDs)"| VER["EvidenceVerifier (V1-V6)"]
    REG --> VER

    VER -->|"best supported hypothesis"| PROP["PROPOSING: provider drafts one fix"]
    PROP --> POL{"Safety policy"}
    POL -->|"DENY"| BLK["BLOCKED"]
    POL -->|"APPROVAL_REQUIRED"| APR["ApprovalStore: exact, single-use; no wins"]
    OP -->|"approve / reject"| APR
    APR -->|"approved"| MIT["MITIGATING: gateway write path"]
    MIT -->|"tool + signed capability"| SRV
    SRV -->|"execute_authorized (verifies the capability)"| ENV

    ENV -.->|"health_check: never an MCP tool"| ORA["Recovery oracle"]
    ORA -->|"yes / no only"| CTL
    CTL -->|"terminal state"| CON["Consolidator"]
    CON -->|"ExperienceEpisode (success only if verified)"| MEMS

    CTL --> OUT["Outputs: RunRecord, trace events, evidence, approvals log, episode"]
```

Read it top to bottom: an operator starts a run; the controller resets the target and turns
its alert into an incident; it looks up runbooks and past incidents; agents investigate with
read-only tools; the verifier decides which root cause the evidence supports; one fix is
proposed, judged and (when required) approved by a human; the gateway executes it with a
signed, single-use capability; the oracle checks recovery on its own channel; the run is
remembered.

---

## 2. Where data comes in, and how far it is trusted

| Input | Enters at | Trusted? | What protects the rest of the system |
|---|---|---|---|
| `configs/*.yaml` (budgets, agents, policies, providers, environment) | `load_settings` ([recap/core/config.py](../recap/core/config.py)) | **yes**: RECAP's own configuration | it is the source of truth for tool risk tiers, allowlists and limits |
| A scenario file (`fixtures/scenarios/*.yaml`) | `SimulatedEnvironment.reset` | the fault is trusted; its `family` and `expected` fields are **evaluation labels** | labels never leave the evaluation code: providers get label-free views (section 4) |
| The alert (service, symptoms) | `AlertSignal` from `reset` | **no**: alert text is data | wrapped as untrusted data in prompts |
| Tool output (logs, metrics, events, health, ...) | `ToolGateway._record` | **no**: a log line can say anything | redacted, bounded, flagged for injection text, stored as `UNTRUSTED` evidence; the verifier lets platform signals outrank log text (V6) |
| Runbook chunks | `BM25Retriever.search` | **no** (text) | wrapped as untrusted data; their action hints only suggest, the policy decides |
| Past episodes | `ExperienceStore.search` | partly: outcome and verification are RECAP's own | "similar" is not "safe to reuse" (`assess_reuse`); providers see label-free views |
| Provider decisions (tool calls, hypotheses, fix drafts) | `AgentHarness`, `IncidentController._propose` | **no** (an LLM may be manipulated) | the gateway's policy, the verifier and the approval decide; declared risk is ignored |
| A human decision | `ApprovalStore.decide` (CLI console, or `POST /approvals/{id}`) | yes, once authenticated | API: operator bearer token, identity from the token; "no" wins until the action runs |
| Recovery oracle output | `EnvironmentHealthOracle.probe` | yes, but **private** | only its yes/no verdict reaches the controller's decisions; its findings never reach a provider |

---

## 3. Stage by stage

Each stage lists its **input → code → output → next**. Trace event categories are in
`code font`; every one is stored in `data/recap.db` (`events`) and `data/traces/<run_id>.jsonl`
and streamed live to the dashboard.

### Stage 0. Start a run

- **Input:** a scenario ID and a `RunConfig` (strategy, RAG/memory switches, approval mode,
  budget), from `scripts/demo.py`, `POST /incidents/run` (operator token required) or the
  evaluation runner.
- **Code:** `Runtime.run_incident` ([recap/runtime.py](../recap/runtime.py)) builds the
  per-run `IncidentController` from long-lived dependencies (`ControllerDeps`).
- **Output:** a run ID (`run-…`). The API returns it at once (202) and runs the incident as
  a background task (`RunManager` in [apps/api/service.py](../apps/api/service.py)).
- **Next:** stage 1.

### Stage 1. Reset the target and open the incident (CREATED)

- **Input:** the scenario ID and its seed.
- **Code:** `IncidentController.run`.
  1. `environment.reset(scenario, seed)` injects the fault into a fresh world
     (`data/sandbox/<run_id>.db` for the simulator) and returns an `AlertSignal`.
  2. The alert becomes an `Incident` (`incident_id`, service, symptoms, time, budget). Its
     `scenario_id` stays in trusted code only.
  3. A fresh Ed25519 key pair is made for this run (`CapabilityIssuer`,
     [recap/safety/capabilities.py](../recap/safety/capabilities.py)). Only the public half
     goes to the environment: `environment.bind_executor(public_key)`.
  4. A `BudgetTracker` starts the run's clock and counters; a `RunRecord` checkpoint is
     saved; the tracer opens the run's trace.
  5. If the alert's namespace is not allowlisted, the run goes straight to BLOCKED.
- **Output:** `state.transition` (to CREATED, with the incident and environment), the
  first checkpoint in `runs`.
- **Next:** the MCP transport starts (stage 2).

### Stage 2. Start the MCP tool server and compare its catalog

- **Input:** the environment config and run ID.
- **Code:** `McpToolClient.stdio` starts `python -m recap.tools.mcp_server --env-config … --run-id …`
  ([recap/tools/mcp_server.py](../recap/tools/mcp_server.py)). The subprocess builds its own
  environment object for the same run, so it shares the sandbox state file. The gateway
  asks for the tool list and compares it with the trusted registry
  ([recap/tools/registry.py](../recap/tools/registry.py)).
- **Output:** `tool.discovery` (advertised, usable, unknown and mismatched tools). What the
  server says decides nothing.
- **Next:** stage 3.

### Stage 3. Look things up (RETRIEVING)

- **Input:** the incident's service and symptoms.
- **Code:** `_retrieve`: BM25 search over the runbooks
  ([recap/knowledge/](../recap/knowledge/)) and a similarity search over remembered
  episodes the run may read ([recap/memory/store.py](../recap/memory/store.py)).
- **Output:** `RetrievedChunk`s and `MemoryHit`s on the run record; `retrieval.docs` and
  `memory.search` events.
- **Next:** both are handed to the agent harness for stage 4.

### Stage 4. Investigate (INVESTIGATING)

- **Input:** routing signals (services affected, memory hits, budget left, prior failures).
- **Code:**
  1. `RulesRouter.decide` ([recap/routing/router.py](../recap/routing/router.py)) picks one
     generalist or two specialists in parallel → `routing.decision`, `investigation.round`.
  2. The `Orchestrator` runs the agents (concurrently in parallel mode), each with a
     deadline: its own limit or the time left on the run, whichever is shorter.
  3. Each agent runs the harness loop ([recap/harness/loop.py](../recap/harness/loop.py)),
     one **step** at a time:
     - **observe:** `ContextPacker.build` ([recap/context/packer.py](../recap/context/packer.py))
       turns the run's evidence into structured summaries
       ([recap/context/summaries.py](../recap/context/summaries.py)), adds runbook chunks
       and label-free memory views, ranks and fits them into the token budget →
       `context.pack`;
     - **reason:** the provider receives an `InvestigationRequest` (label-free
       `AgentIncident`, the pack, the tools it may call, its history) and returns an
       `InvestigationDecision` → `agent.decision`;
     - **act:** each requested read goes through `ToolGateway.call_read`
       ([recap/tools/gateway.py](../recap/tools/gateway.py)): policy (`policy.decision`),
       budget reservation, `tool.call`, the MCP call with a hard timeout, then the result is
       redacted, bounded (keeping the head and the tail), flagged and stored as `Evidence`
       → `tool.result`.
  4. When the provider finishes, it returns hypotheses that cite evidence IDs →
     `agent.finish`; the orchestrator emits `orchestration.summary` (with the measured
     overlap of parallel agents).
- **Output:** one `AgentFinding` per agent; evidence records in `data/recap.db` (`evidence`).
- **Next:** stage 5.

In the example run the investigator made 7 reads: health, logs, the error-rate metric,
deployments, events, the memory metric, and a rollback dry run (`propose_rollback`).

### Stage 5. Verify the evidence

- **Input:** the findings of this epoch, every piece of evidence of the run, the incident,
  and `remediated` (the time of each fix already executed in this run).
- **Code:** `EvidenceVerifier.verify` ([recap/agents/verifier.py](../recap/agents/verifier.py))
  applies rules V1 to V6: structure, deploy timing, upstream dominance, resource pressure,
  dependency failure, remembered claims, and conflict resolution (platform signals outrank
  log text). After a fix, the fixed service's older state observations no longer count.
- **Output:** a `VerificationReport`: every hypothesis supported, contradicted or
  insufficient (never "proven"), the best actionable one, and where to look next →
  `verification.evidence`. Memory hits are re-assessed against it (`memory.assessment`).
- **Next:** a supported best hypothesis goes to stage 6; otherwise another round of stage 4
  (bounded by `max_investigation_rounds`), then FAILED.

### Stage 6. Propose one fix (PROPOSING)

- **Input:** the best hypothesis and the verification report.
- **Code:** `_propose`.
  1. Agentic RAG: a remediation runbook is fetched if none for this mechanism is loaded.
  2. A proposal pack is built; the evidence the diagnosis rests on gets priority, and the
     runbook chunks for the diagnosed mechanism are **pinned** so they can never be crowded
     out → `context.pack` (stage `proposal`).
  3. The provider receives a `ProposalRequest` and returns one `MitigationDraft`, within
     the run's deadline.
  4. The draft becomes a `MitigationProposal` with an ID and a digest (a SHA-256
     fingerprint of incident, action, target and params) → `mitigation.proposed`.
  5. The safety policy judges it from trusted inputs only → `policy.decision`
     ([recap/safety/policy.py](../recap/safety/policy.py)). DENY ends the run BLOCKED.
  6. The verifier checks the proposal cites real evidence and targets the diagnosis →
     `verification.evidence`.
- **Output:** a proposal that needs approval (or, only in the simulation-only safety-off
  ablation, is allowed directly).
- **Next:** stage 7.

### Stage 7. Get an exact approval (AWAITING_APPROVAL)

- **Input:** the proposal.
- **Code:** `_authorize` and the `ApprovalStore`
  ([recap/safety/approvals.py](../recap/safety/approvals.py)).
  1. A request is opened for that exact proposal and digest, with an expiry →
     `approval.requested`, and a `requested` entry in the append-only decision log.
  2. A channel asks for a decision: the console (`y`/`N`), the dashboard through
     `POST /approvals/{id}` (operator token; the approver recorded is `operator:<name>`), or
     the simulation-only auto-approver. The run's clock is paused while a human decides.
  3. The store's rules: approve only a pending request; reject a pending request **or**
     revoke an approval that was not used yet; nothing after a rejection; every change is a
     compare-and-swap in one SQLite transaction; every attempt is logged.
- **Output:** an `Approval` (approved or rejected) → `approval.decided`.
- **Next:** approved → stage 8; rejected or expired → BLOCKED.

### Stage 8. Execute through the gateway (MITIGATING)

- **Input:** the proposal and its approval.
- **Code:** `ToolGateway.execute_mitigation`.
  1. The oracle takes a "before" probe.
  2. The approval is **re-read** from the store; the policy judges the call again with it →
     `policy.decision`.
  3. A lock on the target, a budget reservation, then the approval is **consumed** (single
     use, in the same transaction that checks it is still approved).
  4. The gateway signs a **capability** for exactly this environment action (run, action,
     target, params, approval, digest, a nonce, 60 s expiry).
  5. The MCP write tool passes it to `execute_authorized`; the environment verifies the
     signature against the bound public key, the exact action, the expiry and the nonce, and
     records the nonce in the same transaction as the change. Anything else is refused and
     changes nothing.
- **Output:** an `ActionOutcome` (stored as evidence) → `mitigation.executed`. The
  capability itself is never traced or stored.
- **Next:** stage 9.

### Stage 9. Check recovery (VERIFYING)

- **Input:** the "before" probe.
- **Code:** `_await_recovery`: the oracle
  ([recap/verification/oracle.py](../recap/verification/oracle.py)) calls `health_check()`
  on the controller's own environment object (never an MCP tool). A simulated target is
  judged at once; a live target is probed every `stabilization_poll_seconds` until it is
  healthy, its `stabilization_timeout_seconds` are over, or the run's time runs out.
- **Output:** a `RecoveryResult` → `verification.recovery` (with the number of probes and
  the time waited).
- **Next:** verified → RESOLVED. Not verified → a new **epoch**: the next round's focus is
  the failed fix's target plus the verifier's own focus services (never the oracle's list),
  the fixed service's pre-fix state stops counting, and the run returns to stage 4, at most
  `max_mitigation_attempts` fixes in total.

### Stage 10. Remember and summarize (terminal state)

- **Input:** the final run record and its evidence.
- **Code:** `_consolidate` and `Consolidator.build_episode`
  ([recap/memory/consolidation.py](../recap/memory/consolidation.py)).
- **Output:** an `ExperienceEpisode` in `data/memory.db` (outcome `success` only if the
  oracle verified recovery; never for a held-out test scenario) → `memory.consolidated`;
  then `run.summary` (state, attempts, unsafe actions, budget, cost label) and the final
  checkpoint.

---

## 4. The boundaries, and what is allowed to cross them

```mermaid
flowchart LR
    subgraph TRUSTED["Trusted code (controller process)"]
        CTL["Controller"] --- GW["Gateway + policy"]
        CTL --- VER["Verifier"]
        CTL --- ORA["Oracle"]
        CTL --- KEY["Private signing key"]
    end
    PRV["Provider"] -- "label-free views, summaries, tool specs" --- CTL
    CTL -- "tool calls; writes with a signed capability" --> SRV["MCP server process"]
    SRV --> ENV["Environment: verifies capabilities"]
    ORA -. "health_check" .-> ENV
    API["HTTP API"] -- "operator token; loopback Host; same-site Origin" --- CTL
```

| Boundary | What may cross | What may not | Enforced in |
|---|---|---|---|
| Controller → provider | the alert as `AgentIncident`, packed evidence summaries, runbook chunks, `AgentMemoryHit`s, trusted tool descriptions, failed actions | `scenario_id`, `failure_family`, `partition`, provenance, anything from the oracle | `models.py` (views and validators), `loop.py`, `_propose`, `_retry_focus` |
| Gateway → MCP server → environment | validated read calls; writes that carry a capability for exactly that action | unknown tools or arguments; a write without a valid, unused, unexpired capability | `gateway.py`, `mcp_server.py` (`StrictToolServer`), `capabilities.py`, the adapter's `execute_authorized` |
| Environment → oracle → controller | a yes/no verdict and its signals for the run record and trace | the oracle's findings as hints for agents | `controller.py` |
| Browser / HTTP client → API | reads; state-changing calls with the operator token from a loopback Host and a same-site Origin | anonymous writes, a client-chosen approver identity, foreign hosts (DNS rebinding) and origins | `apps/api/auth.py`, `apps/api/main.py` |

---

## 5. Where every output ends up

| Output | Stored in | Served at | Shown on the dashboard |
|---|---|---|---|
| Run checkpoint (`RunRecord`) | `data/recap.db` → `runs` | `GET /runs`, `GET /runs/{id}` | Overview, Live Incident Run, every run page |
| Trace events | `data/recap.db` → `events`; `data/traces/<run_id>.jsonl` | `GET /runs/{id}/events`, `GET /runs/{id}/stream` (SSE) | Live event feed, Agent Timeline |
| Evidence (raw record + structured data) | `data/recap.db` → `evidence` | inside `GET /runs/{id}` | Evidence & RAG |
| Approval requests, decisions, decision log | `data/recap.db` → `approval_requests`, `approvals`, `approval_events` (append-only) | `GET /approvals/pending`, `GET /approvals/{id}/events`, `GET /approvals/events` | Safety & Approvals, Live Incident Run |
| The simulated world, its action log and used capability nonces | `data/sandbox/<run_id>.db` | not served (oracle and MCP server only) | indirectly, through tool results |
| Experience episodes | `data/memory.db` → `episodes` | `GET /memory` | Experience Memory |
| Operator token | `data/operator-token` (mode 0600) or `RECAP_OPERATOR_TOKEN` | never | typed once into the sign-in box (kept in the tab's sessionStorage) |
| Evaluation reports | `evals/reports/<id>/` and `evals/reports/latest/` | `GET /evals/results` | Evaluations |

---

## 6. The example run, event by event

`bad-deployment`, manual approval approved by the operator, scripted provider, simulated
environment. One line per event (consecutive repeats of a pattern are grouped):

| Stage | Events | What happened |
|---|---|---|
| CREATED | `state.transition`, `tool.discovery` | incident opened; key bound; MCP server started, 10 tools advertised and matched |
| RETRIEVING | `state.transition`, `retrieval.docs`, `memory.search` | 4 runbook chunks; memory searched |
| INVESTIGATING | `state.transition`, `routing.decision`, `investigation.round`, `agent.start` | one investigator (single mode) |
| | 7 × (`context.pack`, `agent.decision`, `policy.decision`, `tool.call`, `tool.result`) | health, logs, error rate, deployments, events, memory, rollback dry run |
| | `context.pack`, `agent.decision`, `agent.finish`, `orchestration.summary` | hypotheses for the verifier |
| | `verification.evidence` | "bad deployment on checkout" supported |
| PROPOSING | `state.transition`, `retrieval.docs`, `context.pack`, `mitigation.proposed`, `policy.decision`, `verification.evidence` | rollback of checkout to v2.4.0, from the runbook; APPROVAL_REQUIRED |
| AWAITING_APPROVAL | `state.transition`, `approval.requested`, `approval.decided` | the operator approved the exact digest |
| MITIGATING | `state.transition`, `policy.decision`, `mitigation.executed` | approval consumed, capability signed and verified, rollback applied |
| VERIFYING | `state.transition`, `verification.recovery` | oracle: unhealthy before, healthy after (1 probe: simulated) |
| RESOLVED | `state.transition`, `memory.consolidated`, `run.summary` | a verified success episode written |

Reproduce it:

```bash
.venv/bin/python scripts/demo.py --scenario bad-deployment --approve ask   # approve with y
```

---

## 7. Which file to open for each step

| Step | File |
|---|---|
| Composition root (what implements each interface) | [recap/runtime.py](../recap/runtime.py) |
| Lifecycle, retries, epochs, stabilization, deadlines | [recap/harness/controller.py](../recap/harness/controller.py), [recap/core/lifecycle.py](../recap/core/lifecycle.py) |
| Agent loop | [recap/harness/loop.py](../recap/harness/loop.py) |
| Budgets | [recap/harness/budget.py](../recap/harness/budget.py) |
| Routing and concurrency | [recap/routing/router.py](../recap/routing/router.py), [recap/routing/orchestrator.py](../recap/routing/orchestrator.py) |
| What a provider sees | [recap/core/models.py](../recap/core/models.py) (views), [recap/context/packer.py](../recap/context/packer.py), [recap/context/summaries.py](../recap/context/summaries.py), [recap/providers/prompts.py](../recap/providers/prompts.py) |
| The scripted provider | [recap/providers/deterministic.py](../recap/providers/deterministic.py), [playbooks.py](../recap/providers/playbooks.py), [heuristics.py](../recap/providers/heuristics.py) |
| Tool boundary | [recap/tools/gateway.py](../recap/tools/gateway.py), [registry.py](../recap/tools/registry.py), [actions.py](../recap/tools/actions.py), [mcp_client.py](../recap/tools/mcp_client.py), [mcp_server.py](../recap/tools/mcp_server.py) |
| Safety | [recap/safety/policy.py](../recap/safety/policy.py), [approvals.py](../recap/safety/approvals.py), [capabilities.py](../recap/safety/capabilities.py) |
| Evidence rules | [recap/agents/verifier.py](../recap/agents/verifier.py) |
| Target environment | [recap/core/interfaces.py](../recap/core/interfaces.py), [recap/environments/simulated/](../recap/environments/simulated/) |
| Recovery oracle | [recap/verification/oracle.py](../recap/verification/oracle.py) |
| Memory | [recap/memory/](../recap/memory/) |
| API and dashboard | [apps/api/](../apps/api/), [apps/dashboard/src/](../apps/dashboard/src/) |
