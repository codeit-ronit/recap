# SREGym adapter

**Status: NOT INTEGRATED.** RECAP V1 has no connection to SREGym, has never run against
it, and has no SREGym results.

The step-by-step plan for a future benchmark run (goals, metrics, leakage controls,
experiment protocol, AWS cost safeguards and reporting rules) is in
[`docs/benchmark-plan.md`](../../docs/benchmark-plan.md). Nothing in it has been run.

## What exists today

`adapter.py` contains `SREGymEnvironment`, a class that implements the same
`TargetEnvironment` interface (`recap/core/interfaces.py`) as the simulator:

| Method | Behaviour in V1 |
|---|---|
| `describe()` | `kind="sregym"`, `name="SREGym (not integrated)"`, `is_simulated=False`, no supported actions |
| `list_scenarios()` | returns `[]` |
| every other method | raises `EnvironmentNotIntegrated` pointing to this file |

Selecting it is a one-line config change (`kind: sregym` in `configs/environment.yaml`;
see `sample_config.yaml`). A run started that way fails immediately and honestly instead of
producing fake observations. `tests/unit/test_sregym_adapter.py` locks this behaviour in,
including that the recovery oracle refuses to report health through this adapter.

The point of the stub is architectural: it shows exactly where real infrastructure plugs
in. Agents, RAG, memory, routing, guardrails and the harness only ever see the observation
models in `recap/core/observations.py`. So swapping the infrastructure means one
`TargetEnvironment` adapter plus `configs/environment.yaml`, and the controller, gateway,
RAG, memory, router and orchestrator *code* stays unchanged. What is still
simulator-specific is *data*, not code: the log conventions that the scripted policy and the
evidence verifier parse (`recap/core/signals.py`), the namespace and service allowlists
(`configs/policies.yaml`) and the runbooks. A real target must adapt those (or use an LLM
provider for its logs). `tests/e2e/test_environment_swap.py` proves the wiring only: its
fake adapter reuses the simulator's conventions.

## What is needed to integrate

1. **Pin an SREGym commit.** Read its license, problem inventory, benchmark rules and
   evaluation stages at that commit. Verify the real tool and API names; do not assume
   they match RECAP's simulator tools.
2. **A cluster.** SREGym problems target live applications on Kubernetes (confirm the exact
   requirements at the pinned commit). Check whether the chosen problems need privileged
   (root) access and whether one VM running Kind (Kubernetes in Docker) is enough.
   Provision one only with the owner's explicit consent, with quotas and costs checked
   first. Budget alerts are not hard spending caps. Keep the benchmark cluster separate
   from RECAP's code and SQLite data (see `infra/README.md`).
3. **The mapping.** Implement each `TargetEnvironment` method by translating real cluster
   data into the observation models. `mapping.md` lists the planned (unverified) mapping
   and the two ways to connect (RECAP's own tools, or the benchmark's tools).
4. **The oracle.** Decide what "recovered" means for each problem (prefer the benchmark's
   own mitigation check where one exists) and implement `health_check()` from ground truth
   that agents cannot reach or influence. The controller probes the oracle as soon as a
   write returns, so a real write must wait for its rollout to finish and settle (or the
   oracle needs a polling rule).
5. **Parsers.** The scripted (non-LLM) policy and the evidence verifier parse the
   simulator's log templates (`recap/core/signals.py`). Real logs need real parsers, or an
   LLM provider that reads free text.
6. **Safety.** Extend the policy for real writes (Kubernetes RBAC, namespace allowlist,
   exact approvals). Until `allowed_namespaces` in `configs/policies.yaml` names the
   benchmark namespace, the controller refuses every run before its first tool call. The
   "safety off" ablation is simulation-only and must never run against a live environment.
   The simulation auto-approver is refused outside a simulated environment too, so every
   live write needs a human approval (console or dashboard).
7. **Tests.** Contract tests for every method against a recorded or live environment,
   including a check that read tools never change cluster state.
8. **Leakage controls.** Before the first scored run, commit a train/test split by failure
   mechanism, keep evaluation memory partitioned the way `evals/runner.py` does, and freeze
   the runbooks. Never write runbooks for test faults.

## The rule

**Never report SREGym results from this repository until this adapter is real**:
implemented, pinned to a specific SREGym commit, and tested. Every V1 number comes from
the local simulator and is labeled simulated.
