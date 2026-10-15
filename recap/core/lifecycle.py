"""The incident lifecycle state machine.

The controller may only move an incident along the arrows below. Anything else
raises IllegalTransition, which keeps runs honest: for example, an incident can
never jump from INVESTIGATING straight to RESOLVED without a verified mitigation.

    CREATED -> RETRIEVING -> INVESTIGATING -> PROPOSING -> AWAITING_APPROVAL
            -> MITIGATING -> VERIFYING -> RESOLVED
    VERIFYING -> INVESTIGATING          (mitigation did not recover; bounded retry)
    any non-terminal -> FAILED | BUDGET_EXHAUSTED
    PROPOSING / AWAITING_APPROVAL / MITIGATING -> BLOCKED   (policy deny, rejection, expiry)
    CREATED -> BLOCKED                                       (refused before starting, e.g. the
                                                              namespace is not allowlisted)
"""

from __future__ import annotations

from recap.core.errors import IllegalTransition
from recap.core.ids import utcnow
from recap.core.models import TERMINAL_STATES, IncidentState, StateChange

S = IncidentState

# Every non-terminal state may also fail or run out of budget.
_ALWAYS_ALLOWED = {S.FAILED, S.BUDGET_EXHAUSTED}

TRANSITIONS: dict[IncidentState, set[IncidentState]] = {
    S.CREATED: {S.RETRIEVING, S.BLOCKED},
    S.RETRIEVING: {S.INVESTIGATING},
    S.INVESTIGATING: {S.PROPOSING},
    S.PROPOSING: {S.AWAITING_APPROVAL, S.MITIGATING, S.BLOCKED},
    S.AWAITING_APPROVAL: {S.MITIGATING, S.BLOCKED},
    S.MITIGATING: {S.VERIFYING, S.BLOCKED},
    S.VERIFYING: {S.RESOLVED, S.INVESTIGATING},
    S.RESOLVED: set(),
    S.FAILED: set(),
    S.BLOCKED: set(),
    S.BUDGET_EXHAUSTED: set(),
}


def allowed_next(state: IncidentState) -> set[IncidentState]:
    """States reachable from `state` in one legal move."""
    if state in TERMINAL_STATES:
        return set()
    return TRANSITIONS[state] | _ALWAYS_ALLOWED


class Lifecycle:
    """Tracks the current state and the full history of one incident."""

    def __init__(self) -> None:
        self.history: list[StateChange] = [
            StateChange(state=S.CREATED, at=utcnow(), reason="incident created")
        ]

    @property
    def state(self) -> IncidentState:
        return self.history[-1].state

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, new_state: IncidentState, reason: str = "") -> StateChange:
        """Move to `new_state` or raise IllegalTransition if the move is not allowed."""
        if new_state not in allowed_next(self.state):
            raise IllegalTransition(f"{self.state} -> {new_state} is not allowed")
        change = StateChange(state=new_state, at=utcnow(), reason=reason)
        self.history.append(change)
        return change
