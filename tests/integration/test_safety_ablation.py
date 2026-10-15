"""The simulation-only 'safety off' ablation, and the guard that keeps it simulation-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from recap.core.config import EnvironmentConfig, Settings
from recap.core.errors import ConfigError
from recap.core.models import EnvironmentInfo, IncidentState, RunConfig
from recap.runtime import Runtime
from tests.fakes import FakeEnvironment


class RealLookingEnvironment(FakeEnvironment):
    """Pretends to be a real (non-simulated) system, to test the guard."""

    def describe(self) -> EnvironmentInfo:
        return super().describe().model_copy(update={"is_simulated": False, "kind": "live"})


async def test_without_the_gateway_the_injected_action_destroys_the_sandbox(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    record = await runtime.run_incident(
        "malicious-log",
        RunConfig(approval_mode="auto", safety_enabled=False, memory_write=False),
    )
    # The compromised planner's delete_namespace actually ran - in the sandbox only.
    executed = [a for a in record.actions if a.action == "delete_namespace"]
    assert executed and executed[0].result != "denied"
    assert record.state != IncidentState.RESOLVED
    assert record.recovery is not None and not record.recovery.verified


async def test_safety_cannot_be_disabled_outside_simulation(
    settings: Settings, tmp_path: Path
) -> None:
    live = settings.model_copy(
        update={
            "environment": EnvironmentConfig(
                kind="tests.integration.test_safety_ablation:RealLookingEnvironment",
                options={"state_dir": str(tmp_path)},
            )
        }
    )
    runtime = Runtime(live)
    with pytest.raises(ConfigError):
        await runtime.run_incident("fake-bad-release", RunConfig(safety_enabled=False))
    with pytest.raises(ConfigError):
        await runtime.run_incident("fake-bad-release", RunConfig(approval_mode="auto"))
