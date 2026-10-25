# `recap/routing`: how much investigation an incident gets

```
incident + memory hits + budget
   └─> signals.py   compute_signals  -> RoutingSignals
         └─> router.py   RulesRouter.decide -> StrategyDecision (mode, rule_id, reason, agents, step budget)
               └─> orchestrator.py   Orchestrator.execute -> one AgentFinding per agent -> EvidenceVerifier
```

Parallel investigation costs more (two agents, more tool calls), so RECAP spends it only when
the signals say an incident is hard. This is a simple, inspectable form of adaptive
inference-time compute. Nothing here is learned.

## Signals (`signals.py`)

| Signal | Meaning |
|---|---|
| `services_affected` | distinct services named by the alert |
| `symptom_categories` | kinds of symptoms: `errors`, `latency`, `availability`, `resource` |
| `familiar_success` | a past run on the same service, similarity ≥ 0.6, recovered and verified by the oracle |
| `memory_conflict` | past incidents with similarity ≥ 0.45 disagree on mechanism or outcome |
| `prior_failed_attempts` | fixes that already failed in this run, plus very similar past runs that failed |
| `escalation` | the previous round ended without a supported diagnosis |
| `disagreement` | the previous round left an open question: a disagreement between agents that no evidence rule settled. The verifier's leads (below) never set it |
| `remaining_*`, `max_parallel_agents` | read from the run's budget tracker |

Thresholds live in `configs/budget.yaml` (`routing:`).

## Rules (`router.py`): the first match wins

| Rule | When | Mode |
|---|---|---|
| `RT0-forced` | the run config says `single` or `parallel` (parallel still needs room for two agents) | forced |
| `RT1-budget-floor` | fewer than `parallel_min_tool_calls` tool calls left, or only one agent allowed | single |
| `RT2-cross-service` | two or more services affected | parallel |
| `RT3-disagreement` | the last round left claims that conflict and no evidence rule settles them (level supported root causes, or claims naming different bad releases): both specialists get the verifier's open questions and leads | parallel |
| `RT3-escalation` | the last round was inconclusive, or a fix already failed | parallel |
| `RT4-memory-conflict` | similar past incidents disagree | parallel |
| `RT5-ambiguous-symptoms` | symptoms span three or more categories | parallel |
| `RT6-familiar` | a verified fix exists for a very similar incident | single |
| `RT7-default` | a clear single-service incident | single |

Single mode runs the `investigator`; parallel mode runs the `telemetry_investigator` and the
`infrastructure_investigator`. Each agent's step limit is its configured `max_steps`, capped by
its share of the steps left (never below 1, so an empty budget ends the run as
BUDGET_EXHAUSTED rather than as a silent no-op).

`LearnedRouter` is an **interface stub only**: it raises `NotImplementedError`. The V2 plan is a
contextual bandit over these same signals, rewarded for verified recovery and penalised for
tool calls and time. Nothing is trained in V1.

## Orchestrator (`orchestrator.py`)

* **single**: the investigator is awaited directly.
* **parallel**: both specialists start together with `asyncio.gather`.

Agents share nothing mutable. Each gets its own frozen `AgentRunPlan`; evidence is exchanged
only by immutable ID through the append-only evidence registry, and the run budget counts
every step and tool call centrally. A plan's `questions` are the verifier's open questions
from the previous round followed by its leads (for example "checkout's calls to payment fail
and nothing on payment itself is confirmed yet: ..."); both are RECAP's own text, built from
checked values ([recap/agents/README.md](../agents/README.md)).

| Failure | What happens |
|---|---|
| timeout (per agent) | in a parallel round an agent may use at most `parallel_agent_time_share` (0.7, `configs/budget.yaml`) of the time left; past its limit it ends as an `AgentFinding` with `error="timeout after Xs"`, and the other agent's finding survives |
| the run's deadline | each agent's timeout is the shorter of its own limit and the time left on the run (`remaining_seconds`); when the run's clock is what ran out, `BudgetExhausted` is re-raised and the run ends BUDGET_EXHAUSTED; with no time left at all, the agent is not started |
| crash | an `AgentFinding` with `error="agent crashed: <type>: <message>"` |
| `BudgetExhausted` | re-raised once every agent has stopped; findings the other agents completed travel with it (`partial_findings`), so the record keeps them, marked `partial: true`; the controller ends the run as BUDGET_EXHAUSTED |
| cancellation | propagates |

Which agent reaches a shared budget first, and when a clock runs out, is timing, which a
replay cannot reproduce (its answers come at once). So each stop has a place that does not
depend on timing: the agent, and how many checks it had made (turn starts, budget checks,
model and embedding requests; `recap/harness/budget.py`). A recorded run writes each
agent's timeout or deadline cut, and every budget refusal, into its recording at that
place. A replay has no clock and never refuses on its own: it stops exactly where the
recording says, so the same agent gets the same timeout finding, or the run stops with the
same budget message ([ADR 0026](../../docs/decisions/0026-a-replay-stops-where-its-recording-stopped.md)).

If every agent of a round comes back with an error (a timeout or a crash, for example because
the provider API is down), the controller ends the run FAILED with `run.error` instead of
starting another round.

Every round emits one `orchestration.summary` trace event:

```json
{"mode": "parallel", "round": 1, "wall_ms": 43.8, "sum_ms": 77.0, "overlap_ms": 33.2,
 "agents": [{"role": "telemetry_investigator", "started_at": "...", "finished_at": "...",
             "duration_ms": 43.6, "error": null}, ...]}
```

`overlap_ms = sum_ms - wall_ms`. Agents run one after another can never add up to more than
the wall-clock time, so `overlap_ms > 0` is the proof that they really ran at the same time.
