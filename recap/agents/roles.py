"""Agent roles: who investigates an incident, and with which read-only tools.

Where it sits: the router picks roles, the orchestrator runs them, and every role is driven by
the same agent loop (recap/harness/loop.py). A role only changes the goal, the tool list and
the step limit, all read from configs/agents.yaml.

    investigator                 the generalist used in single-agent mode (all read tools)
    telemetry_investigator       health, logs, metrics            } the two specialists that
    infrastructure_investigator  deployments, events, topology    } run side by side in parallel
    evidence_verifier            NOT a looping agent: trusted rule code (recap/agents/verifier.py)
    controller                   the only role that may ever execute a fix (through the gateway)

Every investigation role is read-only: recap/core/config.py refuses to start if one of them
lists a write tool.
"""

from __future__ import annotations

from recap.core.config import AgentSpec, Settings
from recap.core.errors import ConfigError
from recap.core.models import AgentRole

# The two specialists the router selects for a parallel investigation.
SPECIALISTS = (AgentRole.TELEMETRY, AgentRole.INFRASTRUCTURE)
# Every role that runs the agent loop (the verifier is code, so it is not one of them).
INVESTIGATION_ROLES = (AgentRole.INVESTIGATOR, *SPECIALISTS)


def agent_specs(settings: Settings) -> dict[AgentRole, AgentSpec]:
    """The investigation roles (investigator + the two specialists) from configs/agents.yaml."""
    missing = [role.value for role in INVESTIGATION_ROLES if role not in settings.agents]
    if missing:
        raise ConfigError(f"configs/agents.yaml is missing agent roles: {', '.join(missing)}")
    return {role: settings.agents[role] for role in INVESTIGATION_ROLES}
