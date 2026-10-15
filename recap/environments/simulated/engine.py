"""SimulatedEnvironment: the V1 TargetEnvironment, a deterministic simulated shop.

This is V1's replaceable infrastructure adapter. It implements
recap.core.interfaces.TargetEnvironment with three small pieces:

    scenario.py     reads a scenario file (topology, history, faults, alert)
    state_store.py  keeps the run's world as JSON in <state_dir>/<run_id>.db (SQLite)
    physics.py      pure functions that turn that world into logs, metrics, events, health

Process model: the MCP tool server (a subprocess) and the controller's recovery oracle each
build their own SimulatedEnvironment with the same run_id. Every method reads the state
fresh from the shared SQLite file, so both always see the same world.

Nothing here touches a real cluster. It is a simulation and says so in describe().
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from recap.core.interfaces import TargetEnvironment
from recap.core.models import EnvironmentInfo, ScenarioInfo
from recap.core.observations import (
    ActionOutcome,
    AlertSignal,
    AuthorizedAction,
    DeploymentList,
    DeploymentRecord,
    EventList,
    HealthReport,
    HealthSnapshot,
    LogQueryResult,
    MetricName,
    MetricSeries,
    RollbackPlan,
    ServiceInfo,
    ServiceList,
)
from recap.environments.simulated import physics
from recap.environments.simulated.scenario import (
    ScenarioSpec,
    list_scenario_specs,
    load_scenario,
    to_info,
)
from recap.environments.simulated.state_store import NOT_RESET, SandboxStateStore, State

DEFAULT_NAMESPACE = "shop"
SUPPORTED_ACTIONS = ["rollback", "restart", "delete_namespace"]
# The run id becomes a file name, so it may not contain path separators.
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class SimulatedEnvironment(TargetEnvironment):
    """A small simulated microservice shop whose state lives in a per-run SQLite file."""

    kind = "simulated"

    def __init__(
        self,
        run_id: str,
        scenarios_dir: str,
        state_dir: str,
        tool_latency_ms: int = 0,
        write_latency_ms: int = 0,
    ) -> None:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError(f"invalid run_id '{run_id}'")
        self.run_id = run_id
        self.scenarios_dir = Path(scenarios_dir)
        self.tool_latency_ms = tool_latency_ms
        self.write_latency_ms = write_latency_ms
        self.store = SandboxStateStore(Path(state_dir) / f"{run_id}.db")

    # --- lifecycle (trusted) ------------------------------------------------------------

    async def reset(self, scenario_id: str, seed: int) -> AlertSignal:
        """Inject the scenario's fault into a fresh world for this run and return its alert."""
        spec = load_scenario(self.scenarios_dir, scenario_id)
        self.store.save(_initial_state(spec, seed))
        # Only alert-style facts leave the simulator: family/expected stay evaluation-only.
        return AlertSignal(
            service=spec.alert.service,
            affected_services=list(spec.alert.affected_services),
            symptoms=list(spec.alert.symptoms),
            namespace=spec.namespace,
            fired_at=spec.incident_time,
            metadata={"source": "simulated-alertmanager", "namespace": spec.namespace},
        )

    def describe(self) -> EnvironmentInfo:
        """Static description; is_simulated=True is what keeps every result labeled simulated."""
        state = self.store.load()
        return EnvironmentInfo(
            kind=self.kind,
            name="RECAP simulated shop",
            namespace=state["namespace"] if state else DEFAULT_NAMESPACE,
            is_simulated=True,
            supported_actions=list(SUPPORTED_ACTIONS),
            description=(
                "Deterministic local simulation of a five-service shop (state in a per-run "
                "SQLite file). No real cluster is involved."
            ),
            # A fix takes effect the moment it is applied (the clock jumps 5 minutes), so
            # there is nothing to wait for before the oracle's verdict.
            stabilization_timeout_seconds=0.0,
        )

    def list_scenarios(self) -> list[ScenarioInfo]:
        """Every scenario in the scenarios folder, sorted by id."""
        return [to_info(spec) for spec in list_scenario_specs(self.scenarios_dir)]

    # --- observe (read-only; exposed to agents as MCP tools) ----------------------------

    async def observe_services(self) -> ServiceList:
        """Services with their running version, replicas and dependencies."""
        state = await self._read()
        now = state["clock_minute"]
        namespace = state["namespace"]
        if physics.namespace_deleted(state, now):
            return ServiceList(namespace=namespace, services=[])  # nothing is left to list
        services = [
            ServiceInfo(
                name=name,
                namespace=namespace,
                version=physics.version_at(state, name, now),
                replicas=service["replicas"],
                dependencies=list(service["dependencies"]),
            )
            for name, service in state["services"].items()
        ]
        return ServiceList(namespace=namespace, services=services)

    async def observe_health(self, service: str) -> HealthSnapshot:
        """Current status, readiness, error rate and p95 latency (slightly noisy)."""
        state = await self._read(service)
        return physics.health_snapshot(state, service, state["clock_minute"])

    async def observe_logs(self, service: str, limit: int, since_minutes: int) -> LogQueryResult:
        """The newest `limit` lines of the last `since_minutes`, oldest first."""
        state = await self._read(service)
        now = state["clock_minute"]
        lines = physics.log_lines(state, service, start=now - since_minutes, end=now)
        # Keep the newest `limit` lines. (Not lines[-limit:]: lines[-0:] would keep them all.)
        kept = lines[max(0, len(lines) - limit) :]
        return LogQueryResult(
            service=service, lines=kept, total_matched=len(lines), truncated=len(kept) < len(lines)
        )

    async def observe_metrics(
        self, service: str, metric: MetricName, window_minutes: int
    ) -> MetricSeries:
        """One metric every 5 minutes over the window, ending at the current minute."""
        state = await self._read(service)
        return physics.metric_series(state, service, metric, window_minutes, state["clock_minute"])

    async def observe_deployments(self, service: str | None, limit: int) -> DeploymentList:
        """Deployment history (including rollbacks), newest first."""
        state = await self._read(service)
        records = [r for r in reversed(state["deployments"]) if service in (None, r["service"])]
        deployments = [
            DeploymentRecord(
                service=r["service"],
                version=r["version"],
                previous_version=r["previous_version"],
                deployed_at=physics.timestamp(state, r["minute"]),
                status=r["status"],
                change_summary=r["change"],
            )
            for r in records[: max(0, limit)]
        ]
        return DeploymentList(deployments=deployments)

    async def observe_events(self, service: str | None, limit: int) -> EventList:
        """Kubernetes-like events of the last hour, newest first."""
        state = await self._read(service)
        events = physics.cluster_events(state, service, state["clock_minute"])
        return EventList(events=events[: max(0, limit)])

    async def plan_rollback(self, service: str) -> RollbackPlan:
        """Dry run: which version a rollback would restore. Changes nothing."""
        state = await self._read(service)
        now = state["clock_minute"]
        current = physics.version_at(state, service, now)
        if physics.namespace_deleted(state, now):
            reason = f"namespace '{state['namespace']}' was deleted"
            return RollbackPlan(
                service=service, current_version=current, possible=False, reason=reason
            )
        target = physics.rollback_target(state, service, now)
        if target is None:
            reason = f"no earlier version of {service} differs from the running {current}"
            return RollbackPlan(
                service=service, current_version=current, possible=False, reason=reason
            )
        reason = f"{target} is the most recent earlier version (running {current})"
        return RollbackPlan(
            service=service,
            current_version=current,
            target_version=target,
            possible=True,
            reason=reason,
        )

    # --- act (the single write path, reached only after the safety gateway) -------------

    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        """Apply an already-authorized write: load, apply and save in one transaction."""
        await _simulate_latency(self.write_latency_ms)
        return self.store.update(lambda state: physics.apply_action(state, action))

    # --- oracle (trusted; never exposed as an MCP tool) ---------------------------------

    async def health_check(self) -> HealthReport:
        """Noise-free health of every service, read straight from the simulator state."""
        state = self._load()
        return physics.ground_truth(state, state["clock_minute"])

    # --- helpers ------------------------------------------------------------------------

    async def _read(self, service: str | None = None) -> State:
        """Wait the simulated read latency, then load fresh state (another process may write)."""
        await _simulate_latency(self.tool_latency_ms)
        return self._load(service)

    def _load(self, service: str | None = None) -> State:
        state = self.store.load()
        if state is None:
            raise RuntimeError(NOT_RESET)
        if service is not None and service not in state["services"]:
            raise ValueError(f"unknown service '{service}'")
        return state


async def _simulate_latency(milliseconds: int) -> None:
    """Make a call take time like a real API would (0 in tests, so they stay fast)."""
    if milliseconds > 0:
        await asyncio.sleep(milliseconds / 1000)


def _initial_state(spec: ScenarioSpec, seed: int) -> State:
    """Turn a scenario into the world state that physics.py works on."""
    deployments: list[dict[str, Any]] = []
    previous: dict[str, str] = {}
    for entry in sorted(spec.deployments, key=lambda d: -d.minutes_ago):  # oldest first
        deployments.append(
            {
                "service": entry.service,
                "version": entry.version,
                "previous_version": previous.get(entry.service),
                "minute": -entry.minutes_ago,
                "status": "complete",
                "change": entry.change,
            }
        )
        previous[entry.service] = entry.version
    faults = [fault.model_dump() for fault in spec.faults]
    for fault in faults:
        if fault["type"] == "memory_leak":
            fault["cleared_at"] = None  # set when a restart or rollback replaces the pods
    return {
        "scenario_id": spec.id,
        "seed": seed,
        "namespace": spec.namespace,
        "incident_time": spec.incident_time.isoformat(),
        "clock_minute": 0,  # minute 0 = the alert; executed writes move it forward
        "namespace_deleted_at": None,
        "services": {name: service.model_dump() for name, service in spec.services.items()},
        "deployments": deployments,
        "faults": faults,
        "log_lines": [
            {
                "service": line.service,
                "minute": -line.minutes_ago,
                "level": line.level,
                "message": line.message,
            }
            for line in spec.log_lines
        ],
        "restarts": [],
        "action_log": [],  # audit trail of every attempted write
    }
