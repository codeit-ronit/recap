"""Tests for the MCP tool server and client, speaking real MCP inside one process.

`McpToolClient.in_process` uses the SDK's legacy mode: real JSON-RPC messages and the
initialize handshake, carried over memory streams instead of a subprocess pipe. The
environment is the tiny FakeEnvironment from tests/fakes.py, which also shows that the server
works with any TargetEnvironment. The subprocess path is in tests/integration/test_mcp_stdio.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from recap import __version__
from recap.core.errors import ConfigError
from recap.core.interfaces import ToolTransport
from recap.core.observations import (
    ActionOutcome,
    DeploymentList,
    EventList,
    HealthSnapshot,
    LogQueryResult,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)
from recap.tools.mcp_client import McpToolClient
from recap.tools.mcp_server import SERVER_NAME, build_mcp_server, main
from tests.fakes import GOOD, FakeEnvironment
from tests.helpers import capability_args

REQUIRED = object()  # marks an argument that has no default

# The fixed tool catalog (DESIGN.md section 2): each tool's argument names and defaults.
CATALOG: dict[str, dict[str, object]] = {
    "list_services": {},
    "get_service_health": {"service": REQUIRED},
    "get_service_logs": {"service": REQUIRED, "limit": 40, "since_minutes": 60},
    "get_service_metrics": {"service": REQUIRED, "metric": REQUIRED, "window_minutes": 60},
    "get_recent_deployments": {"service": None, "limit": 10},
    "get_k8s_like_events": {"service": None, "limit": 30},
    "propose_rollback": {"service": REQUIRED},
    "execute_rollback": {
        "service": REQUIRED,
        "to_version": REQUIRED,
        "approval_id": "",
        "proposal_digest": "",
        "capability": "",
    },
    "restart_service": {
        "service": REQUIRED,
        "approval_id": "",
        "proposal_digest": "",
        "capability": "",
    },
    "delete_namespace": {
        "namespace": REQUIRED,
        "approval_id": "",
        "proposal_digest": "",
        "capability": "",
    },
}
WRITE_TOOLS = {"execute_rollback", "restart_service", "delete_namespace"}
READ_TOOLS = set(CATALOG) - WRITE_TOOLS

READ_CALLS = [
    ("list_services", {}, ServiceList),
    ("get_service_health", {"service": "checkout"}, HealthSnapshot),
    ("get_service_logs", {"service": "checkout"}, LogQueryResult),
    ("get_service_metrics", {"service": "checkout", "metric": "error_rate"}, MetricSeries),
    ("get_recent_deployments", {}, DeploymentList),
    ("get_k8s_like_events", {"service": "checkout"}, EventList),
    ("propose_rollback", {"service": "checkout"}, RollbackPlan),
]


async def fresh(env_class: type[FakeEnvironment], state_dir: Path) -> FakeEnvironment:
    """A fake environment reset to its broken starting state, stored under `state_dir`."""
    environment = env_class(run_id="run-unit", state_dir=str(state_dir))
    await environment.reset("fake-bad-release", seed=1)
    return environment


def connect(environment: FakeEnvironment) -> McpToolClient:
    """An in-process MCP client for a server that wraps `environment`."""
    return McpToolClient.in_process(build_mcp_server(environment))


def recorded_actions(environment: FakeEnvironment) -> list[dict]:
    """Actions the fake wrote to its shared state file."""
    return json.loads(environment.path.read_text())["actions"]


@pytest.fixture
async def env(tmp_path: Path) -> FakeEnvironment:
    return await fresh(FakeEnvironment, tmp_path)


# --- tool discovery ---------------------------------------------------------------------


async def test_server_offers_exactly_the_catalog_with_its_arguments_and_defaults(env):
    async with connect(env) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == set(CATALOG)
    for name, expected in CATALOG.items():
        schema = tools[name].input_schema
        properties = schema.get("properties", {})
        assert set(properties) == set(expected), name
        required = {arg for arg, default in expected.items() if default is REQUIRED}
        assert set(schema.get("required", [])) == required, name
        for arg, default in expected.items():
            if default is not REQUIRED:
                assert properties[arg]["default"] == default, (name, arg)


async def test_tools_publish_output_schemas_descriptions_and_advisory_hints(env):
    async with connect(env) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert all(tool.description and tool.output_schema for tool in tools.values())
    assert all(tools[name].annotations == {"read_only_hint": True} for name in READ_TOOLS)
    assert all(tools[name].annotations["read_only_hint"] is False for name in WRITE_TOOLS)
    assert tools["delete_namespace"].annotations["destructive_hint"] is True


async def test_client_is_a_tool_transport_that_reports_the_server_and_counts_calls(env):
    client = connect(env)
    assert isinstance(client, ToolTransport)
    assert client.transport_name == "mcp-in-memory"

    async with client:
        await client.call_tool("list_services", {}, timeout_seconds=5)
        await client.call_tool("no_such_tool", {}, timeout_seconds=5)

    assert client.server_info["name"] == SERVER_NAME
    assert client.server_info["version"] == __version__
    assert client.server_info["protocol_version"]
    assert client.calls_made == 2  # failed calls count too


async def test_using_the_client_outside_async_with_is_a_programming_error(env):
    with pytest.raises(RuntimeError, match="not connected"):
        await connect(env).call_tool("list_services", {}, timeout_seconds=5)


# --- read tools -------------------------------------------------------------------------


async def test_health_tool_returns_a_typed_snapshot_plus_a_json_text_copy(env):
    async with connect(env) as client:
        result = await client.call_tool(
            "get_service_health", {"service": "checkout"}, timeout_seconds=5
        )

    assert result.ok and result.error is None
    snapshot = HealthSnapshot.model_validate(result.structured)
    assert snapshot.service == "checkout"
    assert snapshot.error_rate == 0.4
    assert json.loads(result.text) == result.structured


@pytest.mark.parametrize(("tool", "args", "model"), READ_CALLS, ids=[c[0] for c in READ_CALLS])
async def test_each_read_tool_returns_its_observation_model(env, tool, args, model):
    async with connect(env) as client:
        result = await client.call_tool(tool, args, timeout_seconds=5)

    assert result.ok, result.error
    model.model_validate(result.structured)


# --- errors are data, never exceptions --------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_service_logs", {"service": "checkout", "limit": "lots"}),
        ("get_service_health", {"service": 42}),
        ("get_service_health", {}),
        ("get_service_metrics", {"service": "checkout", "metric": "disk_io"}),
    ],
    ids=["text-for-int", "int-for-text", "missing-argument", "unknown-metric"],
)
async def test_invalid_arguments_come_back_as_tool_errors(env, tool, args):
    async with connect(env) as client:
        result = await client.call_tool(tool, args, timeout_seconds=5)

    assert not result.ok
    assert "validation error" in result.error


async def test_unknown_tool_comes_back_as_a_tool_error(env):
    async with connect(env) as client:
        result = await client.call_tool("drop_database", {}, timeout_seconds=5)

    assert not result.ok
    assert "Unknown tool" in result.error


class FailingHealth(FakeEnvironment):
    """observe_health raises, like an adapter asked about a service it does not know."""

    async def observe_health(self, service: str) -> HealthSnapshot:
        raise ValueError(f"unknown service {service!r}")


async def test_environment_errors_become_tool_errors_and_the_server_keeps_serving(tmp_path):
    environment = await fresh(FailingHealth, tmp_path)
    async with connect(environment) as client:
        failed = await client.call_tool("get_service_health", {"service": "nope"}, 5)
        afterwards = await client.call_tool("list_services", {}, 5)

    assert not failed.ok
    assert "ValueError: unknown service 'nope'" in failed.error
    assert afterwards.ok


# --- write tools ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        (
            "execute_rollback",
            {"service": "checkout", "to_version": GOOD},
            {"action": "rollback", "target": "checkout", "params": {"to_version": GOOD}},
        ),
        (
            "restart_service",
            {"service": "payment"},
            {"action": "restart", "target": "payment", "params": {}},
        ),
        (
            "delete_namespace",
            {"namespace": "shop"},
            {"action": "delete_namespace", "target": "shop", "params": {}},
        ),
    ],
    ids=["rollback", "restart", "delete-namespace"],
)
async def test_write_tools_reach_execute_authorized_with_a_valid_capability(
    env, tool, args, expected
):
    audit = {"approval_id": "appr-1", "proposal_digest": "d" * 64}
    async with connect(env) as client:
        result = await client.call_tool(tool, capability_args(env, tool, args, **audit), 5)

    # The MCP call worked; whether the action itself succeeded is in the ActionOutcome.
    assert result.ok
    ActionOutcome.model_validate(result.structured)
    # Both audit fields reach the environment's action log unchanged; the token does not.
    assert recorded_actions(env) == [{**expected, **audit}]


async def test_rollback_through_mcp_repairs_the_fake(env):
    args = {"service": "checkout", "to_version": GOOD}
    async with connect(env) as client:
        result = await client.call_tool(
            "execute_rollback", capability_args(env, "execute_rollback", args), 5
        )

    assert ActionOutcome.model_validate(result.structured).ok
    assert (await env.health_check()).services["checkout"].error_rate == 0.0


async def test_a_write_without_a_capability_changes_nothing(env):
    async with connect(env) as client:
        result = await client.call_tool(
            "execute_rollback", {"service": "checkout", "to_version": GOOD}, 5
        )

    outcome = ActionOutcome.model_validate(result.structured)
    assert outcome.ok is False and "not authorized" in outcome.message
    assert (await env.health_check()).services["checkout"].error_rate == 0.4  # still broken


async def test_every_tool_rejects_an_argument_it_does_not_define(env):
    async with connect(env) as client:
        result = await client.call_tool("list_services", {"verbose": True}, 5)

    assert not result.ok
    assert "list_services does not accept the argument(s): verbose" in result.error


# --- concurrency and timeouts -----------------------------------------------------------


class SlowReads(FakeEnvironment):
    """Health and logs each take 0.2 s, so serial handling would need at least 0.4 s."""

    async def observe_health(self, service: str) -> HealthSnapshot:
        await asyncio.sleep(0.2)
        return await super().observe_health(service)

    async def observe_logs(self, service: str, limit: int, since_minutes: int) -> LogQueryResult:
        await asyncio.sleep(0.2)
        return await super().observe_logs(service, limit, since_minutes)


async def test_server_handles_concurrent_calls_concurrently(tmp_path):
    environment = await fresh(SlowReads, tmp_path)
    async with connect(environment) as client:
        started = time.perf_counter()
        health, logs = await asyncio.gather(
            client.call_tool("get_service_health", {"service": "checkout"}, 5),
            client.call_tool("get_service_logs", {"service": "checkout"}, 5),
        )
        elapsed = time.perf_counter() - started

    assert health.ok and logs.ok
    assert elapsed < 0.35


class HangingHealth(FakeEnvironment):
    """observe_health takes 2 s, far longer than the caller is willing to wait."""

    async def observe_health(self, service: str) -> HealthSnapshot:
        await asyncio.sleep(2)
        return await super().observe_health(service)


async def test_slow_tool_times_out_and_the_connection_stays_usable(tmp_path):
    environment = await fresh(HangingHealth, tmp_path)
    async with connect(environment) as client:
        started = time.perf_counter()
        slow = await client.call_tool("get_service_health", {"service": "checkout"}, 0.2)
        waited = time.perf_counter() - started
        afterwards = await client.call_tool("list_services", {}, 5)

    assert not slow.ok
    assert slow.error == "timeout after 0.2s"
    assert waited < 1.0
    assert afterwards.ok


# --- command line -----------------------------------------------------------------------


def test_main_refuses_an_unknown_environment_kind():
    with pytest.raises(ConfigError, match="unknown environment kind"):
        main(["--env-config", '{"kind": "kubernetes"}', "--run-id", "run-cli"])


def test_server_instructions_follow_the_adapter(tmp_path: Path) -> None:
    """The server never calls a real target 'simulated' (the wording comes from describe())."""

    class LiveLooking(FakeEnvironment):
        def describe(self):  # type: ignore[no-untyped-def]
            return super().describe().model_copy(update={"is_simulated": False, "kind": "live"})

    simulated = build_mcp_server(FakeEnvironment(run_id="i1", state_dir=str(tmp_path)))
    live = build_mcp_server(LiveLooking(run_id="i2", state_dir=str(tmp_path)))
    assert simulated.instructions.startswith("Simulated sandbox tools.")
    assert live.instructions.startswith("Target environment (live) tools.")


async def test_a_tool_registered_any_other_way_is_strict_too(env):
    """Argument names are recorded in add_tool, which every registration path goes through."""
    server = build_mcp_server(env)

    def echo(text: str) -> str:
        return text

    server.add_tool(echo)  # not through the decorator
    async with McpToolClient.in_process(server) as client:
        plain = await client.call_tool("echo", {"text": "hi"}, 5)
        smuggled = await client.call_tool("echo", {"text": "hi", "approval_id": "x"}, 5)

    assert plain.ok
    assert not smuggled.ok and "echo does not accept the argument(s): approval_id" in smuggled.error
