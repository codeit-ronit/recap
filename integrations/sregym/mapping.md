# Planned mapping: RECAP tools to SREGym / Kubernetes capabilities

> **Planned and unverified: pin an SREGym commit and verify its actual tool names before
> implementing.** This table names *kinds of capabilities* (standard Kubernetes and
> Prometheus-style operations), not SREGym APIs. Nothing here has been run.

This mapping is step 5 of the benchmark plan in
[`docs/benchmark-plan.md`](../../docs/benchmark-plan.md).

## Read tools (exposed to agents through MCP)

| RECAP MCP tool | `TargetEnvironment` method | Kind of capability it would map to | Translation notes |
|---|---|---|---|
| `list_services()` | `observe_services()` | List Deployments in the benchmark namespace (image tag as version, replica count) | Dependencies are not stored by Kubernetes; take them from the benchmark app's service graph or from traces |
| `get_service_health(service)` | `observe_health(service)` | Pod listing and status (Running, CrashLoopBackOff), ready replicas, plus a Prometheus-style 5xx-ratio and p95-latency query | Map pod phases and container states onto `ServiceStatus` |
| `get_service_logs(service, limit, since_minutes)` | `observe_logs(...)` | Log retrieval for the service's pods (`kubectl logs` equivalent, or a log backend) | Normalise levels to DEBUG/INFO/WARN/ERROR; keep newest-last order and the `truncated` flag |
| `get_service_metrics(service, metric, window_minutes)` | `observe_metrics(...)` | Prometheus-style range queries at a 5-minute step: error ratio, p95 latency, container memory and its limit, CPU, container restart count | Metric names and labels depend on the app's instrumentation |
| `get_recent_deployments(service, limit)` | `observe_deployments(...)` | Rollout history (ReplicaSet revisions with their images and creation times) | Previous version = the previous revision's image tag |
| `get_k8s_like_events(service, limit)` | `observe_events(...)` | Kubernetes Events in the namespace (OOMKilled, BackOff, Unhealthy, ScalingReplicaSet, Killing, Started) | Real events are aggregated with a `count`; keep newest-first order |
| `propose_rollback(service)` | `plan_rollback(service)` | Read the rollout history and pick the previous revision (a dry run, no side effects) | Must never change cluster state |

## Write tools (reached only after the central safety gateway approved the exact action)

| RECAP MCP tool | `TargetEnvironment` method | Kind of capability it would map to | Translation notes |
|---|---|---|---|
| `execute_rollback(service, to_version)` | `execute_authorized(rollback)` | Rollout undo to a specific revision (`kubectl rollout undo --to-revision` equivalent) | Map `to_version` to a revision number first |
| `restart_service(service)` | `execute_authorized(restart)` | Rolling restart (`kubectl rollout restart` equivalent) | |
| `delete_namespace(namespace)` | `execute_authorized(delete_namespace)` | Namespace deletion | Must stay unimplemented against real infrastructure: the gateway always denies it |

## Trusted methods (controller and oracle only, never MCP tools)

| `TargetEnvironment` method | Kind of capability it would map to |
|---|---|
| `reset(scenario_id, seed)` | The benchmark's problem setup and fault injection for one problem id (verify the real API) |
| `health_check()` | Ground truth for the recovery oracle: the benchmark's own mitigation check if it has one, otherwise readiness plus error rate from metrics |
| `list_scenarios()` | The benchmark's problem inventory at the pinned commit |
| `describe()` | Static facts: kind, cluster/namespace, `is_simulated=False`, supported actions. The controller reads it again after `reset`, so it must report the current problem's namespace |

## Two ways to connect (decide after pinning a commit)

- **Option A: keep RECAP's ten tools** (the tables above). The adapter talks to the
  cluster, and RECAP's MCP server (`recap/tools/mcp_server.py`) stays the same. This is the
  design the stub assumes.
- **Option B: use the benchmark's own tools**, if SREGym gives agents tools and its rules
  require them. The gateway then connects to that server instead of RECAP's. V1's
  `McpToolClient` starts RECAP's own server as a local stdio subprocess, so this needs at
  least a different connection setup, and a new `ToolTransport` if the server is remote
  (for example over HTTP). Every real tool also needs an entry in `configs/policies.yaml`
  with a risk tier chosen by RECAP. Until then `ToolGateway.discover()` reports those tools
  as `unknown_tools`, and they cannot be called. The scripted policy cannot run in this
  option, because its playbooks use RECAP's tool names. The trusted methods (`reset`,
  `health_check`, `list_scenarios`, `describe`) still need the adapter.

## Known gaps the mapping alone does not close

- **Only two remediations.** `remediation_tools` in `configs/policies.yaml` lists
  `execute_rollback` and `restart_service`, and `EnvironmentAction` in
  `recap/core/observations.py` allows only `rollback`, `restart` and `delete_namespace`.
  Supporting a fault that needs another fix (for example a config value) is a code change
  beyond the adapter: at least `EnvironmentAction`, the MCP write tools and
  `configs/policies.yaml`.
- **Pod states.** `ServiceStatus` knows `Running`, `CrashLoopBackOff`, `Deleted` and
  `Unknown`. Other real states (for example `Pending`) have no value of their own; the
  adapter would have to report them as `Unknown` unless the model is extended.
- **Settle time.** In the simulator, an executed rollback or restart moves the simulated
  clock 5 minutes forward, so the effect is visible at once. A real rollout takes real
  time, and the controller probes the oracle as soon as the write returns.
- **Allowlists.** `allowed_namespaces` and `allowed_services` in `configs/policies.yaml`
  name the simulated shop. They must name the benchmark application instead. Until
  `allowed_namespaces` names the benchmark namespace, the controller refuses every run before
  its first tool call (CREATED → BLOCKED); a service outside `allowed_services` is denied on
  every call (R4).

The plan lists these with the other risks: see
[`docs/benchmark-plan.md`](../../docs/benchmark-plan.md).
