"""Recovery oracle: decides whether the environment really recovered after a mitigation.

RECAP never takes anyone's word that an incident is fixed: not the agent's summary and
not the tool server's reply. The oracle asks the environment for its ground-truth health
(`TargetEnvironment.health_check`, a trusted method that is NOT exposed as an MCP tool)
and applies one fixed rule to it. It reads no agent output at all.

Where it sits: the controller calls `probe()` before and after a mitigation and combines
the two probes with `recovery_result()`. Recovery counts as verified only when the system
was unhealthy before and is healthy after, and only a verified recovery may become a
successful experience in memory.
"""

from __future__ import annotations

from recap.core.interfaces import RecoveryOracle, TargetEnvironment
from recap.core.models import OracleProbe, RecoveryResult
from recap.core.observations import GroundTruthHealth


class EnvironmentHealthOracle(RecoveryOracle):
    """Judges health from environment ground truth only (works with any adapter)."""

    def __init__(self, environment: TargetEnvironment, max_error_rate: float = 0.05) -> None:
        self._environment = environment
        self.max_error_rate = max_error_rate  # configs/policies.yaml -> oracle.max_error_rate
        self.name = f"{environment.kind}-health-oracle"

    async def probe(self) -> OracleProbe:
        """Read ground truth once and report whether every service is healthy."""
        report = await self._environment.health_check()
        unhealthy = [
            name for name, health in report.services.items() if not self.is_healthy(health)
        ]
        return OracleProbe(
            # An empty report proves nothing, so it never counts as healthy.
            healthy=bool(report.services) and not unhealthy,
            signals={
                name: health.model_dump(mode="json") for name, health in report.services.items()
            },
            unhealthy_services=unhealthy,
            checked_at=report.checked_at,
            services_checked=list(report.services),
        )

    def is_healthy(self, health: GroundTruthHealth) -> bool:
        """Healthy = Running, every replica ready, and error rate below the threshold."""
        return (
            health.status == "Running"
            and health.ready_replicas == health.replicas
            and health.error_rate < self.max_error_rate
        )


def recovery_result(before: OracleProbe, after: OracleProbe, oracle_name: str) -> RecoveryResult:
    """Combine two probes: recovery is verified only if it was broken and is healthy now."""
    return RecoveryResult(
        healthy_before=before.healthy,
        healthy_after=after.healthy,
        observed_signals={"before": before.signals, "after": after.signals},
        oracle_name=oracle_name,
        verified=after.healthy and not before.healthy,
        checked_at=after.checked_at,
        services_checked=after.services_checked,
    )
