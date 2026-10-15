"""Tests for the recovery oracle (recap/verification/oracle.py).

The oracle's rule is small but load-bearing: only a verified recovery may become a
successful memory. These tests pin down the health rule at its boundaries, the
"was broken and is healthy now" logic of recovery_result(), and that the oracle works
with any TargetEnvironment adapter (here the independent FakeEnvironment from tests/fakes.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from recap.core.interfaces import RecoveryOracle
from recap.core.models import OracleProbe
from recap.core.observations import AuthorizedAction, GroundTruthHealth, HealthReport
from recap.verification.oracle import EnvironmentHealthOracle, recovery_result
from tests.fakes import FakeEnvironment

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)


class ReportEnvironment:
    """Just enough of an environment for the oracle: a fixed ground-truth report."""

    kind = "stub"

    def __init__(self, *services: GroundTruthHealth) -> None:
        self.report = HealthReport(checked_at=T0, services={s.service: s for s in services})

    async def health_check(self) -> HealthReport:
        return self.report


def service(
    name: str = "checkout",
    status: str = "Running",
    ready: int = 2,
    replicas: int = 2,
    error_rate: float = 0.0,
) -> GroundTruthHealth:
    return GroundTruthHealth(
        service=name, status=status, ready_replicas=ready, replicas=replicas, error_rate=error_rate
    )


def probe(healthy: bool, minute: int = 0) -> OracleProbe:
    return OracleProbe(
        healthy=healthy,
        signals={"checkout": {"healthy": healthy}},
        unhealthy_services=[] if healthy else ["checkout"],
        checked_at=T0.replace(minute=minute),
        services_checked=["checkout"],
    )


async def test_running_ready_and_quiet_services_are_healthy() -> None:
    environment = ReportEnvironment(service("checkout"), service("payment", error_rate=0.049))
    result = await EnvironmentHealthOracle(environment).probe()  # type: ignore[arg-type]
    assert result.healthy and result.unhealthy_services == []
    assert result.services_checked == ["checkout", "payment"] and result.checked_at == T0
    assert result.signals["payment"] == service("payment", error_rate=0.049).model_dump(mode="json")


@pytest.mark.parametrize(
    "broken",
    [
        service(status="CrashLoopBackOff", ready=1),
        service(status="Deleted", ready=0, error_rate=1.0),
        service(ready=1),  # one replica not ready
        service(error_rate=0.05),  # the threshold itself is already unhealthy
    ],
    ids=["crash-loop", "deleted", "not-ready", "error-rate-at-threshold"],
)
async def test_any_failed_condition_makes_the_system_unhealthy(broken: GroundTruthHealth) -> None:
    environment = ReportEnvironment(broken, service("payment"))
    result = await EnvironmentHealthOracle(environment).probe()  # type: ignore[arg-type]
    assert not result.healthy and result.unhealthy_services == ["checkout"]


async def test_an_empty_report_is_never_healthy() -> None:
    result = await EnvironmentHealthOracle(ReportEnvironment()).probe()  # type: ignore[arg-type]
    assert not result.healthy and result.services_checked == []


async def test_the_threshold_comes_from_configuration() -> None:
    environment = ReportEnvironment(service(error_rate=0.08))
    strict = EnvironmentHealthOracle(environment, max_error_rate=0.05)  # type: ignore[arg-type]
    lenient = EnvironmentHealthOracle(environment, max_error_rate=0.10)  # type: ignore[arg-type]
    assert not (await strict.probe()).healthy
    assert (await lenient.probe()).healthy


def test_the_oracle_implements_the_interface_and_names_its_source() -> None:
    oracle = EnvironmentHealthOracle(ReportEnvironment())  # type: ignore[arg-type]
    assert isinstance(oracle, RecoveryOracle)
    assert oracle.name == "stub-health-oracle"


@pytest.mark.parametrize(
    ("healthy_before", "healthy_after", "verified"),
    [(False, True, True), (False, False, False), (True, True, False), (True, False, False)],
    ids=["recovered", "still-broken", "was-never-broken", "made-it-worse"],
)
def test_recovery_is_verified_only_when_broken_before_and_healthy_after(
    healthy_before: bool, healthy_after: bool, verified: bool
) -> None:
    result = recovery_result(probe(healthy_before), probe(healthy_after, minute=5), "test-oracle")
    assert result.verified is verified
    assert (result.healthy_before, result.healthy_after) == (healthy_before, healthy_after)


def test_recovery_result_keeps_both_probes_for_the_audit_trail() -> None:
    before, after = probe(False), probe(True, minute=5)
    result = recovery_result(before, after, "test-oracle")
    assert result.observed_signals == {"before": before.signals, "after": after.signals}
    assert result.oracle_name == "test-oracle"
    assert result.checked_at == after.checked_at
    assert result.services_checked == after.services_checked


async def test_the_oracle_works_with_another_adapter(tmp_path: Path) -> None:
    environment = FakeEnvironment(run_id="oracle", state_dir=str(tmp_path))
    await environment.reset("fake-bad-release", seed=1)
    oracle = EnvironmentHealthOracle(environment)
    assert oracle.name == "fake-health-oracle"

    before = await oracle.probe()
    action = AuthorizedAction(action="rollback", target="checkout", params={"to_version": "v1.9.0"})
    await environment.execute_authorized(action)
    after = await oracle.probe()
    assert not before.healthy and after.healthy
    assert recovery_result(before, after, oracle.name).verified
