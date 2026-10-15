"""Exception types shared across RECAP.

The controller catches these to move an incident into the right terminal state
(e.g. BudgetExhausted -> BUDGET_EXHAUSTED) instead of crashing.
"""

from __future__ import annotations


class RecapError(Exception):
    """Base class for all RECAP errors."""


class ConfigError(RecapError):
    """A configuration file or run option is invalid or unsafe."""


class IllegalTransition(RecapError):
    """The incident lifecycle was asked to make a move that is not allowed."""


class BudgetExhausted(RecapError):
    """A run hit one of its limits (steps, tool calls, tokens or time)."""

    def __init__(self, limit: str, detail: str = "") -> None:
        self.limit = limit
        self.detail = detail
        super().__init__(f"budget exhausted: {limit}" + (f" ({detail})" if detail else ""))


class ApprovalError(RecapError):
    """An approval was missing, wrong, expired or already used."""


class CapabilityError(RecapError):
    """A write reached the execution boundary without a valid capability for that exact action."""


class EnvironmentNotIntegrated(RecapError):
    """An environment adapter exists only as an interface and is not connected yet."""
