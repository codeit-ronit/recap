"""Proof that the infrastructure is a replaceable adapter.

The same controller, agents, MCP server/client, gateway, verifier, memory and oracle
run unchanged against `tests.fakes.FakeEnvironment` - a different TargetEnvironment
implementation. Only the environment config changes. This proves the wiring; the fake
reuses the simulator's log conventions and the `shop` namespace, as a real adapter would
have to map its data to them (or use an LLM provider).
"""

from __future__ import annotations

from pathlib import Path

from recap.core.config import EnvironmentConfig, Settings
from recap.core.models import IncidentState, RunConfig
from recap.runtime import Runtime


async def test_full_pipeline_runs_on_a_different_environment(
    settings: Settings, tmp_path: Path
) -> None:
    swapped = settings.model_copy(
        update={
            "environment": EnvironmentConfig(
                kind="tests.fakes:FakeEnvironment", options={"state_dir": str(tmp_path / "fake")}
            )
        }
    )
    runtime = Runtime(swapped)
    assert runtime.environment_info().kind == "fake"

    record = await runtime.run_incident("fake-bad-release", RunConfig(approval_mode="auto"))

    assert record.state == IncidentState.RESOLVED
    assert record.environment and record.environment.kind == "fake"
    assert record.versions["transport"] == "mcp-stdio"
    assert record.proposals[-1].action == "execute_rollback"
    assert record.recovery and record.recovery.oracle_name == "fake-health-oracle"
