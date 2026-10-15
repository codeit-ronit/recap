"""One mapping from RECAP's write tools to the environment action each one performs.

Both sides of the execution boundary use it, so they always agree on what is being done:

    gateway     signs a capability for to_environment_action(tool, args)
    MCP server  builds the AuthorizedAction from to_environment_action(tool, args)

If the two ever disagreed, the environment would refuse the write (the capability would
name a different action, target or parameters), which fails safe.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from recap.core.observations import EnvironmentAction


class WriteTool(NamedTuple):
    """How one write tool maps onto an environment action."""

    action: EnvironmentAction
    target_arg: str  # the tool argument that names the target
    param_args: tuple[str, ...]  # tool arguments passed on as action parameters


WRITE_TOOLS: dict[str, WriteTool] = {
    "execute_rollback": WriteTool("rollback", "service", ("to_version",)),
    "restart_service": WriteTool("restart", "service", ()),
    "delete_namespace": WriteTool("delete_namespace", "namespace", ()),
}


def to_environment_action(
    tool_name: str, args: Mapping[str, Any]
) -> tuple[EnvironmentAction, str, dict[str, Any]]:
    """(action, target, params) for a write tool call; KeyError for any other tool.

    e.g. ("execute_rollback", {"service": "checkout", "to_version": "v2.4.0"})
      -> ("rollback", "checkout", {"to_version": "v2.4.0"})
    """
    tool = WRITE_TOOLS[tool_name]
    params = {name: args[name] for name in tool.param_args if name in args}
    return tool.action, str(args[tool.target_arg]), params
