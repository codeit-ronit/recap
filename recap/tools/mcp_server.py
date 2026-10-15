"""RECAP's MCP tool server: publishes one TargetEnvironment as ten real MCP tools.

Where it sits in the architecture:

    ToolGateway -> McpToolClient --(MCP, JSON-RPC over stdio)--> [this server] -> TargetEnvironment

Each tool is a thin, typed wrapper around one TargetEnvironment method and returns an
observation model from recap/core/observations.py. Because of those return types the SDK
publishes a JSON output schema for every tool and sends each result as structured content
(plus a JSON text copy for clients that only read text).

This server does NOT enforce safety. Risk tiers, approvals, budgets and argument allow-lists
live in the central gateway (configs/policies.yaml): an MCP transport is not a security
boundary, and everything a server says about itself is untrusted.

The runtime starts it as a subprocess (see McpToolClient.stdio):

    python -m recap.tools.mcp_server --env-config '{"kind": "simulated", "options": {}}' --run-id r1

It builds its own environment from the same config and run id as the controller, so the tools
(in this process) and the recovery oracle (in the controller) read the same sandbox state.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Awaitable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from recap import __version__
from recap.core.config import EnvironmentConfig
from recap.core.interfaces import TargetEnvironment
from recap.core.models import EnvironmentInfo
from recap.core.observations import (
    ActionOutcome,
    AuthorizedAction,
    DeploymentList,
    EnvironmentAction,
    EventList,
    HealthSnapshot,
    LogQueryResult,
    MetricName,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)
from recap.environments.registry import create_environment

SERVER_NAME = "recap-sandbox-tools"
SERVER_INSTRUCTIONS = (
    "{target} tools. Read tools are side-effect free. Write tools must only be called "
    "by the RECAP controller after the central safety gateway authorized the exact action; "
    "this server does not enforce policy."
)


def server_instructions(info: EnvironmentInfo) -> str:
    """The server's self-description, worded from the adapter (simulated or not)."""
    target = "Simulated sandbox" if info.is_simulated else f"Target environment ({info.kind})"
    return SERVER_INSTRUCTIONS.format(target=target)


# Tool annotations are advisory hints that any server can set to anything, so they are
# untrusted by design: RECAP's gateway never bases a safety decision on them and takes risk
# tiers from configs/policies.yaml instead. They are published only for generic MCP clients.
READ_ONLY = ToolAnnotations(read_only_hint=True)

T = TypeVar("T")


def _write_hints(*, destructive: bool, idempotent: bool) -> ToolAnnotations:
    """Advisory hints for a write tool (untrusted, like all annotations)."""
    return ToolAnnotations(
        read_only_hint=False, destructive_hint=destructive, idempotent_hint=idempotent
    )


async def _guarded(call: Awaitable[T]) -> T:
    """Await an environment call; turn any failure into a readable MCP tool error."""
    try:
        return await call
    except Exception as exc:
        # The SDK already turns exceptions into is_error results (the server never crashes), but
        # it hides their text. Environment failures such as an unknown service are expected, so
        # the caller gets a short reason it can record and act on.
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def build_mcp_server(environment: TargetEnvironment) -> MCPServer:
    """Create an MCP server whose ten tools observe or change `environment`."""
    server = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=server_instructions(environment.describe()),
        log_level="WARNING",
    )
    _register_read_tools(server, environment)
    _register_write_tools(server, environment)
    return server


# Each tool's docstring becomes its MCP description: it is what a model reads about the tool.


def _register_read_tools(server: MCPServer, env: TargetEnvironment) -> None:
    """Register the seven side-effect-free tools (observations and the rollback dry run)."""

    @server.tool(annotations=READ_ONLY)
    async def list_services() -> ServiceList:
        """List services in the namespace with versions, replicas and dependencies."""
        return await _guarded(env.observe_services())

    @server.tool(annotations=READ_ONLY)
    async def get_service_health(service: str) -> HealthSnapshot:
        """Current status, ready replicas, error rate and p95 latency of one service."""
        return await _guarded(env.observe_health(service))

    @server.tool(annotations=READ_ONLY)
    async def get_service_logs(
        service: str, limit: int = 40, since_minutes: int = 60
    ) -> LogQueryResult:
        """Recent log lines of one service (chronological, newest last)."""
        return await _guarded(env.observe_logs(service, limit, since_minutes))

    @server.tool(annotations=READ_ONLY)
    async def get_service_metrics(
        service: str, metric: MetricName, window_minutes: int = 60
    ) -> MetricSeries:
        """A metric time series (5-minute steps) for one service."""
        return await _guarded(env.observe_metrics(service, metric, window_minutes))

    @server.tool(annotations=READ_ONLY)
    async def get_recent_deployments(service: str | None = None, limit: int = 10) -> DeploymentList:
        """Recent deployments, newest first (optionally for one service)."""
        return await _guarded(env.observe_deployments(service, limit))

    @server.tool(annotations=READ_ONLY)
    async def get_k8s_like_events(service: str | None = None, limit: int = 30) -> EventList:
        """Kubernetes-like cluster events such as OOMKilled or BackOff, newest first."""
        return await _guarded(env.observe_events(service, limit))

    @server.tool(annotations=READ_ONLY)
    async def propose_rollback(service: str) -> RollbackPlan:
        """Dry run: which version a rollback of this service would restore, with no side effects."""
        return await _guarded(env.plan_rollback(service))


def _register_write_tools(server: MCPServer, env: TargetEnvironment) -> None:
    """Register the three write tools; all of them use the single write path, execute_authorized."""

    async def execute(
        action: EnvironmentAction,
        target: str,
        params: dict[str, Any],
        approval_id: str,
        proposal_digest: str,
    ) -> ActionOutcome:
        # approval_id and proposal_digest are carried for the environment's audit trail only;
        # the gateway already enforced the approval before calling this tool.
        authorized = AuthorizedAction(
            action=action,
            target=target,
            params=params,
            approval_id=approval_id,
            proposal_digest=proposal_digest,
        )
        return await _guarded(env.execute_authorized(authorized))

    @server.tool(annotations=_write_hints(destructive=False, idempotent=True))
    async def execute_rollback(
        service: str, to_version: str, approval_id: str = "", proposal_digest: str = ""
    ) -> ActionOutcome:
        """Roll a service back to an earlier version (requires an exact approval)."""
        params = {"to_version": to_version}
        return await execute("rollback", service, params, approval_id, proposal_digest)

    @server.tool(annotations=_write_hints(destructive=False, idempotent=False))
    async def restart_service(
        service: str, approval_id: str = "", proposal_digest: str = ""
    ) -> ActionOutcome:
        """Rolling restart of a service's pods (requires an exact approval)."""
        return await execute("restart", service, {}, approval_id, proposal_digest)

    @server.tool(annotations=_write_hints(destructive=True, idempotent=True))
    async def delete_namespace(
        namespace: str, approval_id: str = "", proposal_digest: str = ""
    ) -> ActionOutcome:
        """Delete every service in a namespace (destructive; the gateway always denies it)."""
        return await execute("delete_namespace", namespace, {}, approval_id, proposal_digest)


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point: build the configured environment and serve it over stdio."""
    parser = argparse.ArgumentParser(
        prog="python -m recap.tools.mcp_server",
        description="RECAP MCP tool server (stdio transport).",
    )
    parser.add_argument("--env-config", required=True, help="EnvironmentConfig as a JSON object")
    parser.add_argument("--run-id", required=True, help="run whose sandbox state the tools use")
    args = parser.parse_args(argv)

    # stdout is the MCP protocol channel, so logs must go to stderr (warnings and errors only).
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    config = EnvironmentConfig.model_validate_json(args.env_config)
    environment = create_environment(config, run_id=args.run_id)
    build_mcp_server(environment).run("stdio")


if __name__ == "__main__":
    main()
