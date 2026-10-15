# `recap/harness` — controller, agent loop, budgets, checkpoints

The harness is the part of RECAP that *drives* an incident. Everything else (RAG, memory,
routing, agents, tools, safety, oracle) is a plug-in that the harness calls through an
interface.

| File | What it does |
|---|---|
| `controller.py` | `IncidentController` walks the lifecycle state machine, saves a checkpoint after every transition, routes each investigation round, enforces bounded retries, and is the **only** component that can execute a mitigation (through the safety gateway). |
| `loop.py` | `AgentHarness.run_agent` — the observe → reason → act loop every investigator runs. |
| `budget.py` | `BudgetTracker` — steps, tool calls, estimated tokens and wall-clock time. Tool calls are *reserved before they are sent*, so no call is ever untracked. |
| `store.py` | `RunStore` — SQLite persistence for `RunRecord` checkpoints and evidence. |

## Lifecycle

```
CREATED → RETRIEVING → INVESTIGATING → PROPOSING → AWAITING_APPROVAL → MITIGATING → VERIFYING → RESOLVED
                            ↑                                                         │
                            └──────────── mitigation did not recover (bounded) ───────┘
any active state → FAILED | BUDGET_EXHAUSTED        PROPOSING / AWAITING_APPROVAL / MITIGATING → BLOCKED
CREATED → BLOCKED   (refused before starting, e.g. the namespace is not allowlisted)
```

Legal moves are defined once in `recap/core/lifecycle.py`; an illegal move raises.

## What happens in each stage

Before the first stage, the controller resets the environment (the scenario's fault is
injected and its alert becomes the incident), reads `describe()` again and refuses a run
whose namespace is not in the policy's `allowed_namespaces` (or differs from the alert's):
CREATED → BLOCKED, before any tool call.

1. **RETRIEVING** — BM25 search over runbooks (if RAG is on) and a similarity search over
   past episodes (if memory is on). Skips are traced with a reason.
2. **INVESTIGATING** — the router picks *single* or *parallel* from observable signals; the
   orchestrator runs the agents (parallel = `asyncio.gather`); the evidence verifier checks
   every hypothesis against the evidence registry. If evidence is insufficient the next
   round escalates (more compute, focused on the services the verifier names). Providers
   receive label-free views (`AgentIncident`, `AgentMemoryHit`) and evidence summaries; no
   agent may outlive the run's own deadline.
3. **PROPOSING** — if no runbook for the diagnosed mechanism is loaded yet, it is retrieved
   now ("agentic RAG"), and the runbook chunks for the diagnosed mechanism are pinned in the
   proposal pack. The provider drafts a fix (bounded by the run's deadline); the **safety
   policy** judges it first (hard gate), then the **verifier** checks that it cites real
   evidence and targets the diagnosed service.
4. **AWAITING_APPROVAL** — reversible writes need an approval bound to the exact proposal
   digest. The run budget clock is paused while a human decides. Rejection (or a revocation
   before the approval was used) or expiry → BLOCKED.
5. **MITIGATING** — `gateway.execute_mitigation` re-checks the policy with the approval,
   consumes it (single use), signs a single-use capability for exactly this action with the
   run's key (bound to the environment right after `reset`) and sends the write over MCP. The
   environment verifies the capability before changing anything.
6. **VERIFYING** — the independent oracle compares ground-truth health before and after. A
   live target is probed every `stabilization_poll_seconds` until it is healthy or its
   `stabilization_timeout_seconds` (or the run's time) are over; a simulated one once.
   Not recovered → a new investigation epoch (bounded by `max_mitigation_attempts`): the
   focus is the failed fix's target plus the verifier's focus (never the oracle's list), and
   the verifier ignores the fixed service's state observations from before the fix.
7. **Consolidation** — a finished run becomes an experience episode; `success` only if
   the oracle verified recovery. Nothing is written when the run's memory writes are off
   (evaluation scoring), when it ended by an error or a cancellation, or when it ran a
   held-out test scenario (the controller traces `memory.skip` with the reason).

## Failure handling

- `BudgetExhausted` → `BUDGET_EXHAUSTED` (no further tool calls, no claimed recovery). That
  includes the token budget: context packs are always built at their configured size, and
  `charge_tokens` stops the run when the next pack no longer fits. It also includes the wall
  clock: a provider call (investigation or proposal) that is still running at the run's
  deadline is cut off there.
- Every agent of an investigation round failed (for example the provider API is down) →
  `FAILED` with `run.error` set, instead of another round or an "insufficient evidence" note.
- Any other exception → `FAILED` with the error, redacted, in `run.error`.
- Cancellation → `FAILED`, then re-raised. A pending approval request is expired first, so it
  can never be approved later.
- A console answer that arrives after the request expired → `BLOCKED` ("approval request
  expired").
- Provider usage is counted the moment each decision is made (`AgentHarness.on_usage`), so it
  survives timeouts, crashes and budget stops. Runs that end by an error or a cancellation
  are not consolidated into memory.
