# Environments: the replaceable infrastructure adapter

RECAP never talks to infrastructure directly. Everything that knows *what the target
system is* lives behind one interface, `TargetEnvironment` in `recap/core/interfaces.py`.
An **environment adapter** is a class that implements it and translates its
infrastructure into the observation models in `recap/core/observations.py`
(`HealthSnapshot`, `LogQueryResult`, `MetricSeries`, `ClusterEvent`, ...).

Agents, RAG, memory, routing, guardrails and the harness only ever see those models, so
switching from the simulator to SREGym/Kubernetes later means writing one adapter and
changing `configs/environment.yaml`: the controller, gateway, RAG, memory, router and
orchestrator *code* stays the same. What stays simulator-specific is *data*: the log
conventions the scripted policy and the verifier parse (`recap/core/signals.py`), the
namespace/service allowlists in `configs/policies.yaml`, and the runbooks. A real target must
map its logs to those conventions (or use an LLM provider) and update allowlists and runbooks.

| Adapter | Where | Status |
|---|---|---|
| `SimulatedEnvironment` (`kind: simulated`) | `recap/environments/simulated/` | V1 default: deterministic local simulation |
| `SREGymEnvironment` (`kind: sregym`) | `integrations/sregym/` | Interface only, **not integrated**: every operation raises `EnvironmentNotIntegrated` |
| `FakeEnvironment` (`kind: tests.fakes:FakeEnvironment`) | `tests/fakes.py` | A tiny second adapter used by tests to prove the interface is swappable |

## The contract and the trust boundary

Methods are split by who may call them. This split is the trust boundary.

| Method | Who calls it | Exposed as MCP tool | Purpose |
|---|---|---|---|
| `reset(scenario_id, seed)` | controller (trusted) | no | Start a fresh incident and return its alert |
| `describe()` | controller, policy (trusted) | no | Facts about the target, including `is_simulated` and the current incident's namespace (the controller reads it again after `reset`) |
| `list_scenarios()` | controller, API, evals (trusted) | no | Scenario catalog |
| `health_check()` | recovery oracle (trusted) | **never** | Noise-free ground truth used to verify recovery |
| `observe_services()` | agents | `list_services` | Services, versions, replicas, dependencies |
| `observe_health(service)` | agents | `get_service_health` | Status, readiness, error rate, p95 latency |
| `observe_logs(service, limit, since_minutes)` | agents | `get_service_logs` | Log lines, oldest first, newest `limit` kept |
| `observe_metrics(service, metric, window_minutes)` | agents | `get_service_metrics` | One metric, a point every 5 minutes |
| `observe_deployments(service, limit)` | agents | `get_recent_deployments` | Deployment history, newest first |
| `observe_events(service, limit)` | agents | `get_k8s_like_events` | Kubernetes-like events, newest first |
| `plan_rollback(service)` | agents | `propose_rollback` | Dry run: which version a rollback would restore |
| `bind_executor(public_key)` | the controller, right after `reset` | never | Trust capabilities signed by this run's key |
| `execute_authorized(action)` | MCP write tools, **only with a valid capability** | `execute_rollback`, `restart_service`, `delete_namespace` | The single write path |

* The `observe_*` methods and `plan_rollback` are read-only. Agents reach them only through
  MCP tools; they never hold an environment object.
* `execute_authorized` is the only way to change anything, and it is the final privilege
  barrier. The central safety gateway decides first; then the environment itself refuses the
  write unless it carries a valid capability for exactly that action, signed by the key the
  controller bound with `bind_executor` after reset (`recap/safety/capabilities.py`). The
  check and the change happen in one transaction, and each capability's nonce is recorded,
  so a capability works once. The approval id and proposal digest travel with the action for
  the audit log (the capability binds them too).
* `bind_executor(public_key)` is trusted, controller-only, like `reset`. A new adapter must
  implement both halves: store the key where every process serving the run can read it, and
  verify capabilities before acting (a real adapter must refuse safety-off capabilities).
* `health_check` is never an MCP tool. The recovery oracle (`recap/verification/`) calls it
  through the controller's own environment instance, so nothing an agent says or does can
  change the verdict.

## The simulated environment (V1)

```
scenario YAML ──reset──▶ SimulatedEnvironment ──save──▶ <state_dir>/<run_id>.db (SQLite)
                                                              ▲               ▲
          MCP server subprocess: its own SimulatedEnvironment ┘               │
          controller + recovery oracle: their own SimulatedEnvironment ───────┘
```

| File | Role |
|---|---|
| `simulated/scenario.py` | Typed schema of `fixtures/scenarios/*.yaml` (unknown fields rejected) and loaders |
| `simulated/state_store.py` | The run's whole world as one JSON row in its own SQLite file |
| `simulated/physics.py` | Pure functions: state + minute → logs, metrics, events, health; and `apply_action` |
| `simulated/engine.py` | `SimulatedEnvironment`, the `TargetEnvironment` implementation |

**Two processes, one world.** The MCP tool server runs as a subprocess and builds its own
`SimulatedEnvironment`; the controller builds another for the oracle. Both use the same
`run_id`, and every method reads the state *fresh* from `<state_dir>/<run_id>.db`, so they
always agree. A write loads, applies and saves inside one `BEGIN IMMEDIATE` transaction.

**Time.** Minute 0 is the alert (`incident_time`). Reads never move the clock. An executed
write does: rollback and restart take 5 simulated minutes, delete_namespace takes 1, so the
next observations show the effect. Timestamps are `incident_time + minute`; the wall clock
is never used.

**Determinism.** Observations are a pure function of (state, minute, seed). Noise comes from
`random.Random(f"{seed}|{service}|{minute}|{kind}")`, so the same scenario and seed always
produce identical logs, metrics and events, in any process and in any call order.

**Faults** (the rules are in `physics.py`):

| Fault | What you observe | What fixes it |
|---|---|---|
| `bad_release` | errors and high latency while the bad version runs; one ERROR line per minute `"<exception> (version=<v>)"` | rollback to another version (a restart does not) |
| `memory_leak` | memory climbs to the limit; then CrashLoopBackOff, one replica not ready, `OOMKilled`/`BackOff`/`Unhealthy` events, heap warnings and `OutOfMemoryError` lines | restart, or rollback, of that service (both replace the pods) |
| propagation | callers of an erroring dependency inherit 80% of its error rate, their p95 latency rises to about the 3 s upstream timeout (3000 ms + base), and they log `"upstream <dep> call failed: ..."`, while their own pods stay Running and Ready | fixing the dependency |

All fault-driven text uses the templates in `recap/core/signals.py`, which is how the
scripted policy and the evidence verifier can parse it. Scenario `log_lines` add noise,
misleading hints or prompt-injection text verbatim at their minute.

**Agents vs. oracle.** `observe_health` and the metrics include small seeded noise, like a
real dashboard. `health_check` returns the same values without noise for every service.
The oracle only uses `health_check`.

**delete_namespace** exists so the safety gateway's DENY is real, and so the
simulation-only "safety off" ablation can show what an unsafe executed action would do:
every service becomes `Deleted`, nothing new is logged, and further actions are rejected.

## Scenario files

`fixtures/scenarios/<id>.yaml` (the id must equal the file name). `family` and `expected`
are evaluation-only ground truth: `reset()` returns only the alert, whose metadata is just
`{"source": "simulated-alertmanager", "namespace": ...}`. Partitions: `demo` (the six demo
stories), `train` (episodes evaluation may learn from) and `test` (held out).

A scenario file is data, so its `run_overrides` may set only `planner` and `strategy`. The
loader rejects any other key, and the controller applies only those two, so a scenario can
never switch off safety or approvals, change memory isolation or pick the provider. Its
separate `budget:` field sets that scenario's run limits (the `budget-exhausted` demo allows
3 tool calls).

## How to add an adapter

1. Subclass `TargetEnvironment`, set `kind`, and accept `run_id` plus your options in the
   constructor. `recap/environments/registry.py` builds adapters as
   `YourAdapter(run_id=..., **options)` from `configs/environment.yaml`; a
   `kind: package.module:ClassName` entry works without registering anything.
2. Translate your infrastructure into the observation models. Keep the orderings
   (logs oldest first; deployments and events newest first) and raise
   `ValueError("unknown service '<name>'")` for unknown services. The models store every
   timestamp in UTC; one without a timezone is read as UTC (`as_utc` in
   `recap/core/signals.py`).
3. Report the incident's namespace in the alert from `reset` and in `describe()` afterwards.
   The controller refuses the run before any tool call (CREATED → BLOCKED) unless the two
   agree and the namespace is in `allowed_namespaces` in `configs/policies.yaml`.
4. Make state visible to both processes (the MCP server and the controller's oracle): use
   the real system as the source of truth, or a shared store like the simulator does.
5. Implement `health_check()` from ground truth that agents cannot reach.
6. Run the shared tests. `tests/fakes.py` shows a complete minimal adapter.
