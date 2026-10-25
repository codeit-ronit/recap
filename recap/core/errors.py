"""Exception types shared across RECAP.

The controller catches these to move an incident into the right terminal state
(e.g. BudgetExhausted -> BUDGET_EXHAUSTED) instead of crashing.
"""

from __future__ import annotations

from typing import Any


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


class FreeStatusLost(ProviderError):
    """A hosted provider's response did not look like the free plan: it is stopped (quota.py)."""


class ReplayedCut(BaseException):
    """In a replay: the cancellation that cut the recorded run's work short at this point.

    In the recorded run a clock ran out while an agent (or the controller) waited, and its
    work was cancelled: an agent's time share (its finding became a timeout and the run went
    on) or the run's deadline. A replay answers at once, so no clock runs out; the budget
    tracker raises this where the recording says the cut happened (recap/harness/budget.py).

    Like the asyncio.CancelledError it stands for, it is not an Exception, so no handler of
    ordinary errors on the way catches it. The code whose clock cut the original turns it
    into what that cut became (the orchestrator: a timeout finding, or BudgetExhausted when
    the run's deadline ran out).
    """

    def __init__(self, limit: str, detail: str = "", seconds: float | None = None) -> None:
        self.limit = limit  # what ran out: "time_share" or "max_elapsed_seconds"
        self.detail = detail
        self.seconds = seconds  # an agent's own time limit, for its timeout finding
        super().__init__(f"cut short: {limit}" + (f" ({detail})" if detail else ""))


def attach_model_calls(exc: BaseException, calls: list[Any], usage: Any) -> None:
    """Remember on `exc` the model requests a provider had already sent when it raised.

    A decision can fail after requests went out (a 429, a refusal, the free-plan tripwire,
    a model mismatch, a corrective retry the run's model-token budget refuses). Those
    requests may have cost tokens and are evidence of what happened, so the harness and the
    controller record them (`llm.call` events and usage) before letting the error go on.
    `calls` are ModelCalls; `usage` is their Usage.
    """
    exc.model_calls = list(calls)  # type: ignore[attr-defined]
    exc.model_usage = usage  # type: ignore[attr-defined]


def model_calls_of(exc: BaseException) -> tuple[list[Any], Any | None]:
    """The (ModelCalls, Usage) attached by `attach_model_calls`, or ([], None)."""
    return list(getattr(exc, "model_calls", [])), getattr(exc, "model_usage", None)


class ApprovalError(RecapError):
    """An approval was missing, wrong, expired or already used."""


class CapabilityError(RecapError):
    """A write reached the execution boundary without a valid capability for that exact action."""


class EnvironmentNotIntegrated(RecapError):
    """An environment adapter exists only as an interface and is not connected yet."""
