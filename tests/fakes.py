"""A second, tiny TargetEnvironment used to prove the environment is swappable.

`FakeEnvironment` is NOT the simulator: it is a minimal, independent implementation
of the TargetEnvironment interface (two services, one bad release). Tests run the
same MCP server, gateway, agents, verifier and controller against it unchanged,
which demonstrates that replacing the infrastructure only requires a new adapter.

State lives in a small JSON file under `state_dir`, so the MCP server subprocess and
the oracle in the test process see the same world (just like the real simulator).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from recap.core.interfaces import TargetEnvironment
from recap.core.models import EnvironmentInfo, MemoryPartition, ScenarioInfo
from recap.core.observations import (
    ActionOutcome,
    AlertSignal,
    AuthorizedAction,
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    GroundTruthHealth,
    HealthReport,
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricName,
    MetricPoint,
    MetricSeries,
    RollbackPlan,
    ServiceInfo,
    ServiceList,
)
from recap.core.signals import RELEASE_ERROR_TEMPLATE

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
BAD = "v2.0.0"
GOOD = "v1.9.0"


class FakeEnvironment(TargetEnvironment):
    """Two services in namespace 'shop'; checkout has a bad release until rolled back."""

    kind = "fake"

    def __init__(self, run_id: str = "fake", state_dir: str = ".", **_: object) -> None:
        self.run_id = run_id
        self.path = Path(state_dir) / f"fake-{run_id}.json"

    # --- helpers ------------------------------------------------------------------------
    def _state(self) -> dict:
        if not self.path.exists():
            return {"checkout_version": BAD, "actions": []}
        return json.loads(self.path.read_text())

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state))

    def _broken(self) -> bool:
        return self._state()["checkout_version"] == BAD

    # --- lifecycle ----------------------------------------------------------------------
    async def reset(self, scenario_id: str, seed: int) -> AlertSignal:
        self._save({"checkout_version": BAD, "actions": []})
        return AlertSignal(
            service="checkout",
            affected_services=["checkout"],
            symptoms=["checkout: HTTP 5xx ratio above 30%"],
            namespace="shop",
            fired_at=T0,
        )

    def describe(self) -> EnvironmentInfo:
        return EnvironmentInfo(
            kind=self.kind,
            name="fake test environment",
            namespace="shop",
            is_simulated=True,
            supported_actions=["rollback"],
            description="Minimal second adapter used only by tests.",
        )

    def list_scenarios(self) -> list[ScenarioInfo]:
        return [
            ScenarioInfo(
                id="fake-bad-release",
                title="Fake bad release",
                family="bad_deployment",
                partition=MemoryPartition.DEMO,
                alert_service="checkout",
            )
        ]

    # --- observe ------------------------------------------------------------------------
    async def observe_services(self) -> ServiceList:
        return ServiceList(
            namespace="shop",
            services=[
                ServiceInfo(
                    name="checkout",
                    namespace="shop",
                    version=self._state()["checkout_version"],
                    replicas=2,
                    dependencies=["payment"],
                ),
                ServiceInfo(name="payment", namespace="shop", version="v1.0.0", replicas=2),
            ],
        )

    async def observe_health(self, service: str) -> HealthSnapshot:
        bad = service == "checkout" and self._broken()
        return HealthSnapshot(
            service=service,
            status="Running",
            ready_replicas=2,
            replicas=2,
            error_rate=0.4 if bad else 0.0,
            latency_p95_ms=900 if bad else 80,
            observed_at=T0,
        )

    async def observe_logs(self, service: str, limit: int, since_minutes: int) -> LogQueryResult:
        lines = []
        if service == "checkout" and self._broken():
            msg = RELEASE_ERROR_TEMPLATE.format(exception="KeyError in CartTotals", version=BAD)
            lines = [
                LogLine(
                    timestamp=T0 - timedelta(minutes=m), level="ERROR", service=service, message=msg
                )
                for m in range(5, 0, -1)
            ]
        return LogQueryResult(service=service, lines=lines[-limit:], total_matched=len(lines))

    async def observe_metrics(
        self, service: str, metric: MetricName, window_minutes: int
    ) -> MetricSeries:
        points = []
        for minute in range(-window_minutes, 1, 5):
            value = 0.0
            if metric == "error_rate" and service == "checkout" and self._broken() and minute >= -9:
                value = 0.4
            if metric == "memory_mb":
                value = 200.0
            points.append(MetricPoint(timestamp=T0 + timedelta(minutes=minute), value=value))
        return MetricSeries(
            service=service,
            metric=metric,
            unit="",
            points=points,
            limit=512.0 if metric == "memory_mb" else None,
        )

    async def observe_deployments(self, service: str | None, limit: int) -> DeploymentList:
        records = [
            DeploymentRecord(
                service="checkout",
                version=BAD,
                previous_version=GOOD,
                deployed_at=T0 - timedelta(minutes=10),
            ),
            DeploymentRecord(service="checkout", version=GOOD, deployed_at=T0 - timedelta(days=2)),
        ]
        if service:
            records = [r for r in records if r.service == service]
        return DeploymentList(deployments=records[:limit])

    async def observe_events(self, service: str | None, limit: int) -> EventList:
        event = ClusterEvent(
            timestamp=T0 - timedelta(minutes=10),
            type="Normal",
            reason="ScalingReplicaSet",
            object="deployment/checkout",
            service="checkout",
            message="Scaled up replica set checkout to 2",
        )
        return EventList(events=[event] if service in (None, "checkout") else [])

    async def plan_rollback(self, service: str) -> RollbackPlan:
        current = self._state()["checkout_version"] if service == "checkout" else "v1.0.0"
        possible = service == "checkout" and current == BAD
        return RollbackPlan(
            service=service,
            current_version=current,
            target_version=GOOD if possible else None,
            possible=possible,
            reason="previous healthy revision" if possible else "no earlier revision",
        )

    # --- act ----------------------------------------------------------------------------
    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        state = self._state()
        ok = action.action == "rollback" and action.target == "checkout"
        if ok:
            state["checkout_version"] = action.params.get("to_version", GOOD)
        state["actions"].append(action.model_dump())
        self._save(state)
        return ActionOutcome(
            ok=ok,
            action=action.action,
            target=action.target,
            message="rolled back" if ok else "unsupported in fake environment",
            side_effects=[f"checkout -> {state['checkout_version']}"] if ok else [],
            executed_at=T0,
        )

    # --- oracle -------------------------------------------------------------------------
    async def health_check(self) -> HealthReport:
        bad = self._broken()
        return HealthReport(
            checked_at=T0,
            services={
                "checkout": GroundTruthHealth(
                    service="checkout",
                    status="Running",
                    ready_replicas=2,
                    replicas=2,
                    error_rate=0.4 if bad else 0.0,
                ),
                "payment": GroundTruthHealth(
                    service="payment",
                    status="Running",
                    ready_replicas=2,
                    replicas=2,
                    error_rate=0.0,
                ),
            },
        )
