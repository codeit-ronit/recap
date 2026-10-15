# ADR 0006: Use a deterministic simulated environment for V1

**In one sentence:** V1 investigates and repairs a simulated five-service shop that runs on a
laptop in seconds, gives identical results for the same seed, can be broken safely, and is
labeled "simulated" everywhere.

## Status

Accepted.

## Context

RECAP needs a system to investigate and repair. For V1 that system has to:

- run on one laptop with no cloud account, no Kubernetes cluster, no GPU and no cost;
- be **reproducible**, so tests, demos and evaluations give the same result every time;
- be safe to break, including on purpose: the red-team evaluation switches safety off and
  lets a destructive action run, to measure what the gateway prevents;
- provide **ground truth**: a noise-free answer to "is the system healthy?" for the recovery
  oracle (the independent judge of recovery), plus the right answer for each scenario so
  evaluations can be scored.

A real benchmark such as SREGym needs a real cluster, a pinned benchmark version and money.
V1 provisions nothing: any cluster needs the owner's explicit consent and a cost check first
(see [infra/README.md](../../infra/README.md)).

## Decision

- **The simulator.** `SimulatedEnvironment` in
  [recap/environments/simulated/engine.py](../../recap/environments/simulated/engine.py)
  implements the `TargetEnvironment` interface ([ADR 0007](0007-replaceable-environment-adapter.md)).
  It models a shop with five services (frontend, checkout, payment, inventory, cart) in the
  namespace `shop`.
- **Faults.** The rules live in
  [recap/environments/simulated/physics.py](../../recap/environments/simulated/physics.py):
  a bad release (errors tagged with the version; a rollback fixes it, a restart does not), a
  memory leak (memory climbs to the limit, then out-of-memory kills and a crash loop; a
  restart or a rollback of that service fixes it), and propagation (callers of a failing
  service inherit part of its errors, and their p95 latency rises to about the 3-second
  upstream timeout, while their own pods stay healthy).
- **Scenarios.** Fifteen YAML files in [fixtures/scenarios/](../../fixtures/scenarios/): 6
  demo, 3 train and 6 test. Each file's `family` and `expected` fields are evaluation-only
  ground truth; `reset` returns only the alert. A file's `run_overrides` may only choose the
  planner and the investigation strategy, never safety, approvals or the budget.
- **Determinism.** Observations are a pure function of the state, the simulated minute and
  the seed. The clock moves only when a write runs (a rollback or restart takes 5 simulated
  minutes).
- **Ground truth for the oracle.** `health_check` returns noise-free health and is never an
  MCP tool. The agents see slightly noisy values, like a real dashboard.
- **Real consequences inside the sandbox.** `delete_namespace` really deletes every
  simulated service, so a DENY is meaningful, and the safety-off ablation (a run with the
  safety gateway switched off, simulation only) can show the damage. Once the namespace is
  gone, the sandbox refuses every further write.
- **Honest labels.** The environment describes itself with `is_simulated=True`. Demos print
  "SIMULATED LOCAL ENVIRONMENT · V1", and every evaluation report says it is not SREGym and
  not production. The demo, API and dashboard labels come from the adapter, not from
  hard-coded text, so a real adapter is labeled "LIVE ENVIRONMENT (<kind>) · V1".

## Consequences

### Good

- Everything runs in seconds on a laptop, offline and for free.
- The same scenario and seed always give identical observations
  (`tests/unit/test_simulator.py::test_same_seed_gives_identical_observations`).
- Destructive red-team experiments are safe: the worst case deletes a simulated namespace in
  a per-run SQLite file.
- Exact ground truth makes the oracle and the evaluation scores unambiguous.

### Bad

- **It is not real.** Nothing measured on the simulator transfers to production or to
  SREGym. The evaluation is synthetic and tiny (6 test scenarios, 1 red-team scenario), with
  no statistical claims.
- It is a small world: five services, two fault types plus propagation, one namespace.
- Fault messages follow the templates in [recap/core/signals.py](../../recap/core/signals.py),
  and the scripted policy and the evidence verifier parse exactly those templates. Real logs
  would need real parsers or an LLM provider.
- There is a risk of tuning the system to the simulator's habits. Keeping runbooks generic
  and the test split separate reduces it, but cannot remove it.
- Latency is simulated (fixed per call in [configs/environment.yaml](../../configs/environment.yaml)),
  so timing results say little about a real system.

## Alternatives considered

- **SREGym or Kubernetes from the start.** It needs a cluster, cost checks, the owner's
  explicit consent and a pinned benchmark version. It is deferred: the adapter exists only
  as an interface stub ([integrations/sregym/README.md](../../integrations/sregym/README.md)).
- **A containerized demo application with real fault injection.** Heavier and slower, harder
  to make deterministic, and Docker packaging is not built in V1.
- **Replaying recorded incidents.** Recordings cannot react to an action, so the oracle could
  never verify that a fix worked.
