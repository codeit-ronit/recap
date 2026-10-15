"""SREGym adapter: where a real benchmark environment will plug in. NOT INTEGRATED in V1.

This class implements the same TargetEnvironment interface as the simulator, which shows
exactly where real infrastructure would connect without touching agents, RAG, memory,
routing, guardrails or the harness. It describes itself honestly (is_simulated=False, no
supported actions) and every operation raises EnvironmentNotIntegrated, so no observation,
repair or score in this repository can come from SREGym by accident.

What a real implementation needs is listed in integrations/sregym/README.md, and the
planned (unverified) tool mapping is in integrations/sregym/mapping.md.
"""

from __future__ import annotations

from typing import Any

from recap.core.errors import EnvironmentNotIntegrated
from recap.core.interfaces import TargetEnvironment
from recap.core.models import EnvironmentInfo, ScenarioInfo
from recap.core.observations import (
    ActionOutcome,
    AlertSignal,
    AuthorizedAction,
    DeploymentList,
    EventList,
    HealthReport,
    HealthSnapshot,
    LogQueryResult,
    MetricName,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)

README = "integrations/sregym/README.md"


def _not_integrated(operation: str) -> EnvironmentNotIntegrated:
    return EnvironmentNotIntegrated(
        f"SREGym is not integrated in RECAP V1, so '{operation}' is unavailable. See {README}."
    )


class SREGymEnvironment(TargetEnvironment):
    """Interface-only SREGym adapter: honest self-description, every operation refused."""

    kind = "sregym"

    def __init__(self, run_id: str, **options: Any) -> None:
        self.run_id = run_id
        self.options = dict(options)  # e.g. namespace; see sample_config.yaml

    def describe(self) -> EnvironmentInfo:
        """Says plainly that nothing is connected."""
        return EnvironmentInfo(
            kind=self.kind,
            name="SREGym (not integrated)",
            namespace=str(self.options.get("namespace", "default")),
            is_simulated=False,
            supported_actions=[],
            description="Interface only in V1; no connection to SREGym exists.",
        )

    def list_scenarios(self) -> list[ScenarioInfo]:
        """No problems can be listed without a real, pinned SREGym installation."""
        return []

    async def reset(self, scenario_id: str, seed: int) -> AlertSignal:
        raise _not_integrated("reset")

    async def observe_services(self) -> ServiceList:
        raise _not_integrated("observe_services")

    async def observe_health(self, service: str) -> HealthSnapshot:
        raise _not_integrated("observe_health")

    async def observe_logs(self, service: str, limit: int, since_minutes: int) -> LogQueryResult:
        raise _not_integrated("observe_logs")

    async def observe_metrics(
        self, service: str, metric: MetricName, window_minutes: int
    ) -> MetricSeries:
        raise _not_integrated("observe_metrics")

    async def observe_deployments(self, service: str | None, limit: int) -> DeploymentList:
        raise _not_integrated("observe_deployments")

    async def observe_events(self, service: str | None, limit: int) -> EventList:
        raise _not_integrated("observe_events")

    async def plan_rollback(self, service: str) -> RollbackPlan:
        raise _not_integrated("plan_rollback")

    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        raise _not_integrated("execute_authorized")

    async def health_check(self) -> HealthReport:
        raise _not_integrated("health_check")
