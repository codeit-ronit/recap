"""Budget tracker: counts what a run spends and stops it cleanly at a limit.

Every tool call is *reserved* before it is sent. If the reservation fails the call
never happens, so a run can never make an untracked tool call. Exceeding any limit
raises BudgetExhausted, which the controller turns into the BUDGET_EXHAUSTED state.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from recap.core.errors import BudgetExhausted
from recap.core.models import RunBudget


class BudgetTracker:
    def __init__(self, budget: RunBudget, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self._clock = clock
        self._started = clock()
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self.steps = 0
        self.tool_calls = 0
        self.tokens = 0

    # --- charging (each raises BudgetExhausted when a limit would be exceeded) ----------

    def charge_step(self) -> None:
        """Count one provider decision."""
        self.check_deadline()
        if self.steps >= self.budget.max_steps:
            raise BudgetExhausted("max_steps", f"{self.steps}/{self.budget.max_steps} steps used")
        self.steps += 1

    def reserve_tool_call(self) -> None:
        """Count one tool call BEFORE it is sent."""
        self.check_deadline()
        if self.tool_calls >= self.budget.max_tool_calls:
            raise BudgetExhausted(
                "max_tool_calls", f"{self.tool_calls}/{self.budget.max_tool_calls} tool calls used"
            )
        self.tool_calls += 1

    def charge_tokens(self, estimated_tokens: int) -> None:
        """Count estimated context tokens handed to a provider."""
        if self.tokens + estimated_tokens > self.budget.max_estimated_tokens:
            raise BudgetExhausted(
                "max_estimated_tokens",
                f"{self.tokens}+{estimated_tokens} > {self.budget.max_estimated_tokens}",
            )
        self.tokens += estimated_tokens

    def check_deadline(self) -> None:
        if self.elapsed_seconds() > self.budget.max_elapsed_seconds:
            raise BudgetExhausted(
                "max_elapsed_seconds",
                f"{self.elapsed_seconds():.1f}s > {self.budget.max_elapsed_seconds}s",
            )

    # --- time (waiting for a human approval does not count) -----------------------------

    def pause_clock(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._clock()

    def resume_clock(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._clock() - self._paused_at
            self._paused_at = None

    def elapsed_seconds(self) -> float:
        now = self._paused_at if self._paused_at is not None else self._clock()
        return now - self._started - self._paused_total

    # --- reporting -------------------------------------------------------------------------

    def remaining_tool_calls(self) -> int:
        return max(0, self.budget.max_tool_calls - self.tool_calls)

    def remaining_steps(self) -> int:
        return max(0, self.budget.max_steps - self.steps)

    def remaining_tokens(self) -> int:
        return max(0, self.budget.max_estimated_tokens - self.tokens)

    def snapshot(self) -> dict[str, float]:
        return {
            "steps": self.steps,
            "max_steps": self.budget.max_steps,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.budget.max_tool_calls,
            "estimated_tokens": self.tokens,
            "max_estimated_tokens": self.budget.max_estimated_tokens,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "max_elapsed_seconds": self.budget.max_elapsed_seconds,
        }
