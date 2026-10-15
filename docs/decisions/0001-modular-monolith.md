# ADR 0001: Build V1 as a modular monolith

**In one sentence:** RECAP V1 is one Python package with clearly separated modules that talk
only through typed contracts and interfaces, wired together in one place, running in one
process (plus one MCP tool server subprocess per run).

## Status

Accepted.

## Context

V1 has many parts: an incident controller, investigating agents, MCP tools, a safety
gateway, approvals, runbook retrieval, experience memory, routing, tracing, an evaluation
runner, an HTTP API and a dashboard. It is built and maintained by one owner, runs on one
laptop, and has to be easy to read, test and explain.

Several parts must be replaceable later without rewriting the rest: the target environment
(simulator now, SREGym or Kubernetes later), the decision provider (scripted policy now,
an LLM later), the router, the safety policy and the trace sinks.

## Decision

- **One package, one module per concern.**

  | Module | Responsibility |
  |---|---|
  | [recap/core](../../recap/core/) | Shared contracts (`models.py`), interfaces (`interfaces.py`), lifecycle, config |
  | [recap/harness](../../recap/harness/) | Incident controller, agent loop, budgets, run store |
  | [recap/tools](../../recap/tools/) | Tool gateway, trusted registry, MCP server and client |
  | [recap/safety](../../recap/safety/) | Policy, approvals, injection flagging, red-team planner |
  | [recap/environments](../../recap/environments/) | Environment registry and the simulator |
  | [recap/knowledge](../../recap/knowledge/), [recap/memory](../../recap/memory/), [recap/context](../../recap/context/) | Runbook retrieval, experience memory, evidence and context packs |
  | [recap/routing](../../recap/routing/), [recap/agents](../../recap/agents/), [recap/verification](../../recap/verification/) | Single or parallel routing, agent roles and the evidence verifier, the recovery oracle |
  | [recap/providers](../../recap/providers/), [recap/telemetry](../../recap/telemetry/) | Decision providers, traces and redaction |
  | [apps/api](../../apps/api/), [apps/dashboard](../../apps/dashboard/), [evals](../../evals/), [integrations](../../integrations/) | HTTP API, React dashboard, evaluation runner, adapters for outside systems |

- **Contracts and interfaces, not concrete classes.** Data that crosses a module boundary is
  a Pydantic model in [recap/core/models.py](../../recap/core/models.py) (unknown fields are
  rejected). Replaceable parts are interfaces in
  [recap/core/interfaces.py](../../recap/core/interfaces.py): `TargetEnvironment`,
  `ToolTransport`, `LLMProvider`, `RetrievalIndex`, `Reranker`, `RouterPolicy`,
  `SafetyPolicy`, `RecoveryOracle`, `Verifier`, `TraceSink`, `ApprovalChannel`, and the
  unimplemented `TrainingPipeline` placeholder.
- **One composition root.** A *composition root* is the one place that chooses the concrete
  class for every interface. Here it is `Runtime` in
  [recap/runtime.py](../../recap/runtime.py). The CLI demo, the API and the evaluation runner
  all build a `Runtime`.
- **One process, asyncio for concurrency.** Parallel specialist agents run with
  `asyncio.gather` in [recap/routing/orchestrator.py](../../recap/routing/orchestrator.py).
- **One deliberate exception:** the MCP tool server runs as a separate subprocess for each
  run, so the tool boundary is a real protocol boundary
  ([ADR 0004](0004-real-mcp-over-stdio.md)).

## Consequences

### Good

- One install and one command run everything: `.venv/bin/python -m pytest` reports 781
  passed and 1 skipped in about 30 seconds with the optional Anthropic SDK installed, as in
  CI (778 passed and 4 skipped without it). The remaining skip is the optional
  OpenTelemetry test.
- A run is easy to follow: one trace per run, one process to debug, no network between
  components.
- Parts are swapped behind interfaces without touching their callers.
  [tests/e2e/test_environment_swap.py](../../tests/e2e/test_environment_swap.py) runs the
  whole pipeline on a different environment adapter with only a config change
  ([ADR 0007](0007-replaceable-environment-adapter.md)).
- Continuous integration stays simple:
  [.github/workflows/ci.yml](../../.github/workflows/ci.yml) runs lint, tests, a smoke test,
  and the dashboard's tests and build, with no secrets.

### Bad

- No independent scaling and little fault isolation. Apart from the MCP subprocess,
  everything shares one process, so a crash of the API process stops every run in flight.
- Checkpoints are saved after every lifecycle transition, but an interrupted run is not
  resumed (not yet built).
- Python cannot enforce module or capability boundaries. For example, `ReadOnlyTools` is a
  convention backed by the gateway's own checks, and module dependencies are kept clean by
  review (no import-linting tool is configured).
- The live event stream (`BroadcastHub` in
  [recap/telemetry/sinks.py](../../recap/telemetry/sinks.py)) lives in one process. Several
  API workers would need a shared message broker.

## Alternatives considered

- **Microservices** (one service per agent or stage). This adds network calls,
  service-to-service authentication and deployment work, and a single-user local prototype
  has no scaling need that would pay for it.
- **Building the core on an agent framework.** V1's goal is to make the control loop,
  budgets, approvals and the safety path explicit and testable in plain code. The interfaces
  leave room to adopt a framework later for one part, such as a provider.
- **Separate packages or repositories per component.** This means more versioning and
  release work for one owner, and it makes a change that touches several parts harder to
  review.
