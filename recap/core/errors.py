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


class QuotaExhausted(BudgetExhausted):
    """A free hosted model's quota ran out: HTTP 429, or RECAP's own cap below the provider's.

    It is a kind of BudgetExhausted, so everything that already stops cleanly at a budget
    limit (the orchestrator, the controller) also stops cleanly here. The controller then
    ends the run as QUOTA_EXHAUSTED (deferred). RECAP never switches to another provider.
    """

    def __init__(self, provider: str, detail: str = "") -> None:
        super().__init__("provider_quota", f"{provider}: {detail}" if detail else provider)
        self.provider = provider


class ProviderError(RecapError):
    """A language-model provider gave no usable answer: refused, unreachable or misconfigured.

    The run fails with this message. There is never a silent fallback to another provider.
    """


class ReplayMismatch(ProviderError):
    """A replayed run sent a request the recording does not contain (the recording is stale)."""


class ApprovalError(RecapError):
    """An approval was missing, wrong, expired or already used."""


class CapabilityError(RecapError):
    """A write reached the execution boundary without a valid capability for that exact action."""


class EnvironmentNotIntegrated(RecapError):
    """An environment adapter exists only as an interface and is not connected yet."""
