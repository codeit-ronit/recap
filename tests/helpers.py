"""Shared test helpers for code that writes to an environment directly.

Since M1 the write boundary is enforced by the environment itself (audit finding 2.5): a
write needs a capability signed by the key the controller bound after reset. `authorize`
does for a test exactly what RECAP does before every real write:

    the controller, right after reset   bind a fresh run key to the environment
    the gateway, after the policy ALLOW  sign a single-use capability for exactly the action

so tests that drive an environment directly go THROUGH the same boundary, never around it.
"""

from __future__ import annotations

from typing import Any

from recap.core.interfaces import TargetEnvironment
from recap.core.observations import ActionOutcome, AuthorizedAction
from recap.safety.capabilities import CapabilityIssuer
from recap.tools.actions import to_environment_action

TEST_INCIDENT = "inc-test"


def issuer_for(environment: TargetEnvironment) -> CapabilityIssuer:
    """A fresh signing key for the environment's run, bound to the environment (like a reset)."""
    issuer = CapabilityIssuer(_run_id(environment), TEST_INCIDENT)
    environment.bind_executor(issuer.public_key)
    return issuer


def sign(
    issuer: CapabilityIssuer, action: AuthorizedAction, *, safety_enabled: bool = True
) -> AuthorizedAction:
    """`action` carrying a capability for exactly itself (what the gateway attaches)."""
    token = issuer.issue(
        action=action.action,
        target=action.target,
        params=action.params,
        approval_id=action.approval_id,
        proposal_digest=action.proposal_digest,
        safety_enabled=safety_enabled,
    )
    return action.model_copy(update={"capability": token})


def authorize(environment: TargetEnvironment, action: AuthorizedAction) -> AuthorizedAction:
    """Bind a fresh key to `environment` and sign `action` with it."""
    return sign(issuer_for(environment), action)


async def act(environment: TargetEnvironment, action: AuthorizedAction) -> ActionOutcome:
    """Send `action` to the environment the way RECAP does: with a valid capability."""
    return await environment.execute_authorized(authorize(environment, action))


def capability_args(
    environment: TargetEnvironment, tool: str, args: dict[str, Any], **audit: str
) -> dict[str, Any]:
    """MCP arguments for a write tool, plus a valid capability (what the gateway sends)."""
    action, target, params = to_environment_action(tool, args)
    authorized = AuthorizedAction(action=action, target=target, params=params, **audit)
    return {**args, **audit, "capability": authorize(environment, authorized).capability}


def _run_id(environment: TargetEnvironment) -> str:
    run_id = getattr(environment, "run_id", None)
    if not isinstance(run_id, str):
        raise TypeError(f"{type(environment).__name__} has no run_id to sign for")
    return run_id
