"""Budget tracker: counts what a run spends and stops it cleanly at a limit.

Every tool call is *reserved* before it is sent. If the reservation fails the call
never happens, so a run can never make an untracked tool call. Exceeding any limit
raises BudgetExhausted, which the controller turns into the BUDGET_EXHAUSTED state.

Model tokens work the same way: before each request to a model, the provider reserves
what the request could cost (its estimated prompt plus the most it may answer); after the
answer it settles with the tokens the server actually reported. Requests of parallel
agents reserve against the same total, so together they can never overshoot it.

Stops and replays. Which of two parallel agents reaches a shared limit first, and when a
clock runs out, is timing. A replay answers at once, so its agents interleave differently
and no clock of its runs out: it cannot find those stops by itself. So the tracker gives
every stop a place in the run that does not depend on timing:

    (actor, check)  whose turn it was (ACTOR: the agent whose turn the harness is running,
                    or "controller" outside any agent's turn) and how many checks that actor
                    had made: turn starts, budget checks, and model or embedding requests

An agent makes the same checks in the same order in a replay, whatever the other agent does
meanwhile (its prompts do not depend on the other's timing, ADR 0020). A recorded run writes
each stop into its recording at its place:

    refusal  a check refused: any BudgetExhausted raised here, or the orchestrator finding
             no time left to start an agent. The refused check counted nothing.
    cut      a clock cancelled what the actor's last check allowed: an agent's time share or
             the run's deadline ran out while it waited. That check counted; its work never
             finished.

A shared allowance that is not a run limit (the runbook searches of a run, `allow`) is
recorded the same way; its refusal stops nothing.

A replay never refuses on its own and has no clock (its time never runs out): it raises
exactly the recorded stops, at the same places, a refusal as BudgetExhausted and a cut as
ReplayedCut, which the code whose clock cut the original turns back into what that cut
became. Its counters and totals still count, so they end as the original's did.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NoReturn

from recap.core.errors import BudgetExhausted, ReplayedCut, ReplayMismatch
from recap.core.models import AgentRole, RunBudget

# Whose turn it is. The harness sets it for each agent's turn (AgentHarness.run_agent); the
# controller's own work (retrieval, proposals, fixes) runs outside any turn.
ACTOR: ContextVar[str] = ContextVar("recap_actor", default=AgentRole.CONTROLLER.value)

Stop = dict[str, Any]  # one stop as a recording keeps it (see the module doc)
Point = tuple[str, int]  # (actor, check)


@contextmanager
def acting(actor: str) -> Iterator[None]:
    """Count every check made inside as `actor`'s (one agent's turn)."""
    token = ACTOR.set(actor)
    try:
        yield
    finally:
        ACTOR.reset(token)


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
        self.model_tokens = 0  # reported by model servers (an estimate when one reports none)
        self.model_reserved = 0  # reserved by requests still waiting for their answer
        # Stops (see the module doc): every actor's checks so far, what its last check was,
        # and the model tokens it settled since then.
        self._checks: dict[str, int] = {}
        self._last: dict[str, str] = {}
        self._settled: dict[str, int] = {}
        self._write: Callable[[Stop], None] | None = None  # a recorded run's recording
        self._recorded: dict[Point, Stop] | None = None  # a replay's stops, by place

    # --- charging (each raises BudgetExhausted when a limit would be exceeded) ----------

    def charge_step(self) -> None:
        """Count one provider decision."""
        point = self._check("step")
        if not self.replaying:  # a replay refuses only where the recorded run did
            self._refuse_if_late(point)
            if self.steps >= self.budget.max_steps:
                self._refuse(point, "max_steps", f"{self.steps}/{self.budget.max_steps} steps used")
        self.steps += 1
        self._cut_after(point)

    def reserve_tool_call(self) -> None:
        """Count one tool call BEFORE it is sent."""
        point = self._check("tool_call")
        if not self.replaying:
            self._refuse_if_late(point)
            if self.tool_calls >= self.budget.max_tool_calls:
                self._refuse(
                    point,
                    "max_tool_calls",
                    f"{self.tool_calls}/{self.budget.max_tool_calls} tool calls used",
                )
        self.tool_calls += 1
        self._cut_after(point)

    def charge_tokens(self, estimated_tokens: int) -> None:
        """Count estimated context tokens handed to a provider."""
        point = self._check("tokens")
        limit = self.budget.max_estimated_tokens
        if not self.replaying and self.tokens + estimated_tokens > limit:
            self._refuse(
                point, "max_estimated_tokens", f"{self.tokens}+{estimated_tokens} > {limit}"
            )
        self.tokens += estimated_tokens
        self._cut_after(point)

    def reserve_model_tokens(self, tokens: int) -> int:
        """Reserve what one model request could cost BEFORE it is sent; returns the amount."""
        point = self._check("model_tokens")
        limit = self.budget.max_model_tokens
        if not self.replaying and self.model_tokens + self.model_reserved + tokens > limit:
            self._refuse(
                point,
                "max_model_tokens",
                f"{self.model_tokens} used + {self.model_reserved} reserved + {tokens} for "
                f"the next request > {limit}",
            )
        cut = self._recorded_cut(point)
        if cut is not None:
            # The recorded request was cancelled before it went out, and its provider settled
            # it with an estimate: count that, as the original did, instead of reserving.
            self.model_tokens += int(cut.get("settled") or 0)
            raise _replayed(cut)
        self.model_reserved += tokens
        return tokens

    def settle_model_tokens(self, reserved: int, actual: int) -> None:
        """Replace a reservation by what the request really cost (as the server reported)."""
        self.model_reserved = max(0, self.model_reserved - reserved)
        self.model_tokens += max(0, actual)
        actor = ACTOR.get()
        self._settled[actor] = self._settled.get(actor, 0) + max(0, actual)

    def check_deadline(self) -> None:
        """Refuse once the run's wall-clock budget is used up (a replay has no clock)."""
        point = self._check("deadline")
        if not self.replaying:
            self._refuse_if_late(point)
        self._cut_after(point)

    # --- stops: written into a recorded run's recording, raised from it in a replay ------

    def record_stops(self, write: Callable[[Stop], None]) -> None:
        """A recorded run: `write` appends each stop to the run's recording."""
        self._write = write

    def replay_stops(self, stops: Sequence[Mapping[str, Any]]) -> None:
        """A replay: from now on stop exactly where the recorded run did, never on its own."""
        self._recorded = {}
        for stop in stops:
            self._recorded.setdefault((str(stop["actor"]), int(stop["check"])), dict(stop))

    @property
    def replaying(self) -> bool:
        """True in a replay: no refusal of its own, no clock (see the module doc)."""
        return self._recorded is not None

    def begin_turn(self, actor: str) -> None:
        """An agent's turn starts (the orchestrator calls this before starting it).

        A check of its own, so that a stop before any other check of the turn (no time left
        to start the agent, or a cut before its first check) has a place, also when the same
        agent has turns in several rounds."""
        self._cut_after(self._check("turn", actor))

    def check_request(self) -> None:
        """A model or embedding request is about to go out (the run's recording and replay
        transports call this). It counts nothing and never refuses; it places a cut that
        came while a request was out exactly at that request."""
        self._cut_after(self._check("request"))

    def checkpoint(self, name: str = "checkpoint") -> None:
        """A place for a stop after work no other check marks (e.g. a wait for the target
        to settle). It counts nothing; call it at the same point in every run."""
        self._cut_after(self._check(name))

    def record_cut(
        self, kind: str, detail: str, *, actor: str | None = None, seconds: float | None = None
    ) -> None:
        """A clock cancelled what `actor`'s last check allowed (the acting one by default).

        Called by the code whose clock ran out (the orchestrator, the controller's deadline)
        once the cancelled work has unwound. A recorded run writes the cut at that check;
        `seconds` is an agent's own time limit, for its timeout finding."""
        actor = actor or ACTOR.get()
        extra: Stop = {"cut": True}
        if seconds is not None:
            extra["seconds"] = seconds
        if self._last.get(actor) == "model_tokens":
            # Cancelled after its reservation, before its request went out: what the
            # provider settled it with, which a replay counts at that reservation.
            extra["settled"] = self._settled.get(actor, 0)
        self._write_stop((actor, self._checks.get(actor, 0)), kind, detail, extra)

    def record_refusal(self, kind: str, detail: str, *, actor: str | None = None) -> None:
        """A clock refused at `actor`'s last check (the orchestrator found no time left to
        start the agent): a recorded run writes the refusal at that check."""
        actor = actor or ACTOR.get()
        self._write_stop((actor, self._checks.get(actor, 0)), kind, detail, {"cut": False})

    def allow(self, kind: str, allowed: bool, detail: str) -> bool:
        """Whether the acting agent may use a shared allowance that is not a run limit, such
        as the runbook searches of a run; `allowed` is what the caller's own count says. A
        refusal stops nothing: the caller turns it into a failed tool call.

        Which parallel agent finds such an allowance used up is timing, as for a run limit:
        a recorded run writes each refusal at its place, and a replay refuses exactly there,
        whatever its own count says."""
        point = self._check(kind)
        if self._recorded is not None:
            return point not in self._recorded
        if not allowed:
            self._write_stop(point, kind, detail, {"cut": False, "allowance": True})
        return allowed

    def _check(self, name: str, actor: str | None = None) -> Point:
        """Count one check of `actor` (the acting one by default) and return its place.

        In a replay, a refusal recorded here is raised before the check counts anything,
        as in the original."""
        actor = actor or ACTOR.get()
        point = (actor, self._checks.get(actor, 0) + 1)
        self._checks[actor] = point[1]
        self._last[actor] = name
        self._settled[actor] = 0
        stop = self._recorded.get(point) if self._recorded is not None else None
        if stop is None:
            return point
        if stop.get("at") not in (None, name):
            raise ReplayMismatch(
                f"the recording stops {actor} at its check {point[1]} ({stop['at']}), where "
                f"this run made a different check ({name}): the run no longer makes the "
                "checks the recorded one made; record the run again."
            )
        if not stop.get("cut") and not stop.get("allowance"):  # allow() answers those itself
            raise BudgetExhausted(str(stop["kind"]), str(stop.get("detail") or ""))
        return point

    def _recorded_cut(self, point: Point) -> Stop | None:
        stop = self._recorded.get(point) if self._recorded is not None else None
        return stop if stop is not None and stop.get("cut") else None

    def _cut_after(self, point: Point) -> None:
        """In a replay: the recorded run was cut after this check, so cut here too."""
        cut = self._recorded_cut(point)
        if cut is not None:
            raise _replayed(cut)

    def _refuse_if_late(self, point: Point) -> None:
        if self.elapsed_seconds() > self.budget.max_elapsed_seconds:
            limit = self.budget.max_elapsed_seconds
            self._refuse(point, "max_elapsed_seconds", f"{self.elapsed_seconds():.1f}s > {limit}s")

    def _refuse(self, point: Point, kind: str, detail: str) -> NoReturn:
        self._write_stop(point, kind, detail, {"cut": False})
        raise BudgetExhausted(kind, detail)

    def _write_stop(self, point: Point, kind: str, detail: str, extra: Stop) -> None:
        if self._write is None:
            return
        actor, check = point
        at = self._last.get(actor, "")
        self._write(
            {"actor": actor, "check": check, "at": at, "kind": kind, "detail": detail, **extra}
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

    def remaining_seconds(self) -> float:
        """Wall-clock time left before max_elapsed_seconds (approval waits excluded).

        Unbounded in a replay: its deadline stops come from the recording, never its clock."""
        if self.replaying:
            return math.inf
        return max(0.0, self.budget.max_elapsed_seconds - self.elapsed_seconds())

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
            "model_tokens": self.model_tokens,
            "max_model_tokens": self.budget.max_model_tokens,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "max_elapsed_seconds": self.budget.max_elapsed_seconds,
        }


def _replayed(cut: Stop) -> ReplayedCut:
    seconds = cut.get("seconds")
    return ReplayedCut(
        str(cut["kind"]),
        str(cut.get("detail") or ""),
        float(seconds) if isinstance(seconds, int | float) else None,
    )
