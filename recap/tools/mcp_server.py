"""RECAP's MCP tool server: publishes one TargetEnvironment as ten real MCP tools.

Where it sits in the architecture:

    ToolGateway -> McpToolClient --(MCP, JSON-RPC over stdio)--> [this server] -> TargetEnvironment

Each tool is a thin, typed wrapper around one TargetEnvironment method and returns an
observation model from recap/core/observations.py. Because of those return types the SDK
publishes a JSON output schema for every tool and sends each result as structured content
(plus a JSON text copy for clients that only read text).

Policy (risk tiers, approvals, budgets, argument allow-lists) lives in the central gateway
(configs/policies.yaml), and everything a server says about itself is untrusted. But the
server is not a free door either (audit finding 2.5):

  * every write tool passes the caller's `capability` to the environment, which refuses the
    write unless it is a valid signed capability for exactly that action (see
    recap/safety/capabilities.py); an empty or made-up approval ID unlocks nothing;
  * every tool rejects arguments it does not define, instead of silently dropping them.

The runtime starts it as a subprocess (see McpToolClient.stdio):

    python -m recap.tools.mcp_server --env-config '{"kind": "simulated", "options": {}}' --run-id r1

It builds its own environment from the same config and run id as the controller, so the tools
(in this process) and the recovery oracle (in the controller) read the same sandbox state.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import sys
from collections.abc import Awaitable, Callable
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
    EventList,
    HealthSnapshot,
    LogQueryResult,
    MetricName,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)
from recap.environments.registry import create_environment
from recap.tools.actions import to_environment_action

SERVER_NAME = "recap-sandbox-tools"
SERVER_INSTRUCTIONS = (
    "{target} tools. Read tools are side-effect free. Write tools act only with a valid "
    "capability that RECAP's central safety gateway signed for the exact action; any other "
    "write is refused."
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
F = TypeVar("F", bound=Callable[..., Any])  # a tool function


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


class StrictToolServer(MCPServer):
    """An MCPServer whose tools reject arguments they do not define.

    The SDK builds each tool's argument model from the function signature and, by default,
    silently ignores unknown keys. A smuggled argument (an `approval_id` sent to a read tool,
    a misspelled field) would then vanish without a trace. Here it is a tool error that the
    caller sees and RECAP's trace records.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.argument_names: dict[str, frozenset[str]] = {}  # tool -> the arguments it defines

    def strict_tool(self, annotations: ToolAnnotations) -> Callable[[F], F]:
        """Like `server.tool(...)`, but also remembers which arguments the tool accepts."""

        def register(function: F) -> F:
            names = frozenset(inspect.signature(function).parameters)
            self.argument_names[function.__name__] = names
            self.tool(annotations=annotations)(function)
            return function

        return register

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        """Refuse unknown arguments, then let the SDK validate and run the tool as usual."""
        unknown = sorted(set(arguments) - self.argument_names.get(name, frozenset(arguments)))
        if unknown:
            raise ToolError(f"{name} does not accept the argument(s): {', '.join(unknown)}")
        return await super().call_tool(name, arguments, context)


def build_mcp_server(environment: TargetEnvironment) -> StrictToolServer:
    """Create an MCP server whose ten tools observe or change `environment`."""
    server = StrictToolServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=server_instructions(environment.describe()),
        log_level="WARNING",
    )
    _register_read_tools(server, environment)
    _register_write_tools(server, environment)
    return server


# Each tool's docstring becomes its MCP description: it is what a model reads about the tool.


def _register_read_tools(server: StrictToolServer, env: TargetEnvironment) -> None:
    """Register the seven side-effect-free tools (observations and the rollback dry run)."""

    @server.strict_tool(annotations=READ_ONLY)
    async def list_services() -> ServiceList:
        """List services in the namespace with versions, replicas and dependencies."""
        return await _guarded(env.observe_services())

    @server.strict_tool(annotations=READ_ONLY)
    async def get_service_health(service: str) -> HealthSnapshot:
        """Current status, ready replicas, error rate and p95 latency of one service."""
        return await _guarded(env.observe_health(service))

    @server.strict_tool(annotations=READ_ONLY)
    async def get_service_logs(
        service: str, limit: int = 40, since_minutes: int = 60
    ) -> LogQueryResult:
        """Recent log lines of one service (chronological, newest last)."""
        return await _guarded(env.observe_logs(service, limit, since_minutes))

    @server.strict_tool(annotations=READ_ONLY)
    async def get_service_metrics(
        service: str, metric: MetricName, window_minutes: int = 60
    ) -> MetricSeries:
        """A metric time series (5-minute steps) for one service."""
        return await _guarded(env.observe_metrics(service, metric, window_minutes))

    @server.strict_tool(annotations=READ_ONLY)
    async def get_recent_deployments(service: str | None = None, limit: int = 10) -> DeploymentList:
        """Recent deployments, newest first (optionally for one service)."""
        return await _guarded(env.observe_deployments(service, limit))

    @server.strict_tool(annotations=READ_ONLY)
    async def get_k8s_like_events(service: str | None = None, limit: int = 30) -> EventList:
        """Kubernetes-like cluster events such as OOMKilled or BackOff, newest first."""
        return await _guarded(env.observe_events(service, limit))

    @server.strict_tool(annotations=READ_ONLY)
    async def propose_rollback(service: str) -> RollbackPlan:
        """Dry run: which version a rollback of this service would restore, with no side effects."""
        return await _guarded(env.plan_rollback(service))


def _register_write_tools(server: StrictToolServer, env: TargetEnvironment) -> None:
    """Register the three write tools; all of them use the single write path, execute_authorized."""

    async def execute(
        tool: str, args: dict[str, Any], approval_id: str, proposal_digest: str, capability: str
    ) -> ActionOutcome:
        # The same mapping the gateway signed (recap/tools/actions.py), so a valid capability
        # matches exactly. The environment verifies the capability before changing anything.
        action, target, params = to_environment_action(tool, args)
        authorized = AuthorizedAction(
            action=action,
            target=target,
            params=params,
            approval_id=approval_id,
            proposal_digest=proposal_digest,
            capability=capability,
        )
        return await _guarded(env.execute_authorized(authorized))

    @server.strict_tool(annotations=_write_hints(destructive=False, idempotent=True))
    async def execute_rollback(
        service: str,
        to_version: str,
        approval_id: str = "",
        proposal_digest: str = "",
        capability: str = "",
    ) -> ActionOutcome:
        """Roll a service back to an earlier version (needs RECAP's signed capability)."""
        args = {"service": service, "to_version": to_version}
        return await execute("execute_rollback", args, approval_id, proposal_digest, capability)

    @server.strict_tool(annotations=_write_hints(destructive=False, idempotent=False))
    async def restart_service(
        service: str, approval_id: str = "", proposal_digest: str = "", capability: str = ""
    ) -> ActionOutcome:
        """Rolling restart of a service's pods (needs RECAP's signed capability)."""
        args = {"service": service}
        return await execute("restart_service", args, approval_id, proposal_digest, capability)

    @server.strict_tool(annotations=_write_hints(destructive=True, idempotent=True))
    async def delete_namespace(
        namespace: str, approval_id: str = "", proposal_digest: str = "", capability: str = ""
    ) -> ActionOutcome:
        """Delete every service in a namespace (destructive; the gateway always denies it)."""
        args = {"namespace": namespace}
        return await execute("delete_namespace", args, approval_id, proposal_digest, capability)


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
