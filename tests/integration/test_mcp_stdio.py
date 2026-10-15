"""MCP over a real subprocess: the same path the RECAP runtime uses.

`McpToolClient.stdio` starts `python -m recap.tools.mcp_server` and speaks MCP to it over the
child's stdin/stdout. The server process builds its own FakeEnvironment from the config, and
both processes share the fake's state file. After a rollback through MCP, a fresh environment
object in THIS process sees the recovery. That is how the recovery oracle works: it reads
ground truth through its own channel, independent of what the tool channel reported.

The rollback carries a capability signed with a key bound in THIS process (as the
controller does): the server subprocess holds no secret, yet it verifies the capability
against the public key in the shared state file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp import StdioServerParameters

from recap.core.config import REPO_ROOT, EnvironmentConfig
from recap.core.observations import ActionOutcome, HealthSnapshot
from recap.tools.mcp_client import McpToolClient
from recap.tools.mcp_server import SERVER_NAME
from tests.fakes import GOOD, FakeEnvironment
from tests.helpers import capability_args

RUN_ID = "run-stdio"
TIMEOUT_SECONDS = 10.0


def fake_config(state_dir: Path) -> EnvironmentConfig:
    """Config that points the server subprocess at the test-only FakeEnvironment."""
    return EnvironmentConfig(
        kind="tests.fakes:FakeEnvironment", options={"state_dir": str(state_dir)}
    )


async def test_rollback_through_the_mcp_subprocess_changes_the_shared_state(tmp_path: Path):
    environment = FakeEnvironment(run_id=RUN_ID, state_dir=str(tmp_path))
    await environment.reset("fake-bad-release", 1)
    rollback_args = {"service": "checkout", "to_version": GOOD}
    # Bind a key here and sign the rollback, exactly as the controller and gateway do.
    signed = capability_args(
        environment, "execute_rollback", rollback_args, approval_id="appr-test"
    )

    async with McpToolClient.stdio(fake_config(tmp_path), RUN_ID) as client:
        assert client.server_info["name"] == SERVER_NAME
        assert client.server_info["protocol_version"]
        assert len(await client.list_tools()) == 10

        health = await client.call_tool(
            "get_service_health", {"service": "checkout"}, TIMEOUT_SECONDS
        )
        assert health.ok, health.error
        assert HealthSnapshot.model_validate(health.structured).error_rate == 0.4

        rollback = await client.call_tool("execute_rollback", signed, TIMEOUT_SECONDS)
        assert rollback.ok, rollback.error
        assert ActionOutcome.model_validate(rollback.structured).ok
        # The same capability again is refused: it was used up in the subprocess.
        replay = await client.call_tool("execute_rollback", signed, TIMEOUT_SECONDS)
        assert ActionOutcome.model_validate(replay.structured).ok is False

    # Oracle channel: a separate environment object in this process, no MCP involved.
    oracle_view = FakeEnvironment(run_id=RUN_ID, state_dir=str(tmp_path))
    report = await oracle_view.health_check()
    assert report.services["checkout"].error_rate == 0.0
    # The approval id crossed the process boundary into the environment's audit trail, and
    # the capability itself (a bearer token until used) was never stored.
    action, replayed = json.loads(oracle_view.path.read_text())["actions"]
    assert action["approval_id"] == "appr-test" and "capability" not in action
    assert replayed["refused"] is True


def test_stdio_client_starts_the_server_module_without_forwarding_our_environment(
    tmp_path: Path,
):
    client = McpToolClient.stdio(fake_config(tmp_path), RUN_ID)
    params = client.server

    assert isinstance(params, StdioServerParameters)
    assert params.env is None  # so the SDK passes only PATH, HOME, ...: no API keys
    assert params.command == sys.executable
    assert params.args[:2] == ["-m", "recap.tools.mcp_server"]
    config_json = params.args[params.args.index("--env-config") + 1]
    assert EnvironmentConfig.model_validate_json(config_json) == fake_config(tmp_path)
    assert params.args[-2:] == ["--run-id", RUN_ID]
    assert params.cwd == str(REPO_ROOT)
    assert client.transport_name == "mcp-stdio"
