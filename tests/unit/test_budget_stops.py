"""Stops a replay cannot find by itself: recorded where they happened, raised there again.

recap/harness/budget.py. Which agent reaches a shared limit first and when a clock runs out
are timing. A recorded run writes each stop at its place, (actor, that actor's check count);
a replay never refuses on its own and has no clock, and raises exactly the recorded stops.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from recap.core.errors import BudgetExhausted, ReplayedCut, ReplayMismatch
from recap.core.models import RunBudget
from recap.harness.budget import ACTOR, BudgetTracker, acting

TELEMETRY, INFRA = "telemetry_investigator", "infrastructure_investigator"


def recorded(budget: RunBudget, **clock: Any) -> tuple[BudgetTracker, list[dict[str, Any]]]:
    """A tracker of a recorded run, and the stop lines it writes."""
    lines: list[dict[str, Any]] = []
    tracker = BudgetTracker(budget, **clock)
    tracker.record_stops(lines.append)
    return tracker, lines


def replaying(budget: RunBudget, stops: list[dict[str, Any]], **clock: Any) -> BudgetTracker:
    tracker = BudgetTracker(budget, **clock)
    tracker.replay_stops(stops)
    return tracker


def test_the_acting_agent_is_marked_for_its_turn_only() -> None:
    assert ACTOR.get() == "controller"  # outside any agent's turn
    with acting(TELEMETRY):
        assert ACTOR.get() == TELEMETRY
    assert ACTOR.get() == "controller"


def test_a_shared_limit_is_refused_where_the_recording_says_whoever_asks_first() -> None:
    """Live: infrastructure took the last tool call, so telemetry's second call was refused.
    In the replay telemetry asks first; the replay still refuses telemetry's second call,
    at the same place, with the recorded words, and refuses nothing else."""
    budget = RunBudget(max_tool_calls=2)
    live, lines = recorded(budget)
    with acting(TELEMETRY):
        live.reserve_tool_call()
    with acting(INFRA):
        live.reserve_tool_call()
    with acting(TELEMETRY), pytest.raises(BudgetExhausted) as refused:
        live.reserve_tool_call()
    assert lines == [
        {
            "actor": TELEMETRY,
            "check": 2,
            "at": "tool_call",
            "kind": "max_tool_calls",
            "detail": "2/2 tool calls used",
            "cut": False,
        }
    ]

    replay = replaying(budget, lines)
    with acting(TELEMETRY):
        replay.reserve_tool_call()
        with pytest.raises(BudgetExhausted) as again:
            replay.reserve_tool_call()
    with acting(INFRA):
        replay.reserve_tool_call()  # the limit is used up, but this was never refused live
    assert str(again.value) == str(refused.value)
    assert replay.tool_calls == live.tool_calls == 2  # the refused call counted nowhere


def test_a_replay_never_refuses_on_its_own_and_has_no_clock() -> None:
    """A recording made before stops were recorded holds none: the replay refuses nothing,
    and its time never runs out (its deadline stops would come from the recording)."""
    now = [0.0]
    replay = replaying(RunBudget(max_steps=1, max_elapsed_seconds=5), [], clock=lambda: now[0])
    now[0] = 100.0  # far past the deadline
    replay.charge_step()
    replay.charge_step()  # past max_steps too
    replay.check_deadline()
    replay.reserve_model_tokens(10**9)
    assert replay.steps == 2 and replay.remaining_seconds() == math.inf


def test_a_cut_is_written_at_the_last_check_and_replayed_once_that_check_counted() -> None:
    """The time share cut telemetry during its tool call: the call was reserved (counted),
    then cancelled. The replay counts the same call and cuts there."""
    budget = RunBudget()
    live, lines = recorded(budget)
    with acting(TELEMETRY):
        live.charge_step()
        live.reserve_tool_call()
    live.record_cut("time_share", "timeout after 0.5s", actor=TELEMETRY, seconds=0.5)
    assert lines == [
        {
            "actor": TELEMETRY,
            "check": 2,
            "at": "tool_call",
            "kind": "time_share",
            "detail": "timeout after 0.5s",
            "cut": True,
            "seconds": 0.5,
        }
    ]

    replay = replaying(budget, lines)
    with acting(TELEMETRY):
        replay.charge_step()
        with pytest.raises(ReplayedCut) as cut:
            replay.reserve_tool_call()
    assert (cut.value.limit, cut.value.detail, cut.value.seconds) == (
        "time_share",
        "timeout after 0.5s",
        0.5,
    )
    assert (replay.steps, replay.tool_calls) == (live.steps, live.tool_calls) == (1, 1)


def test_a_cut_while_a_request_was_out_is_raised_at_that_request() -> None:
    budget = RunBudget()
    live, lines = recorded(budget)
    with acting(INFRA):
        live.reserve_model_tokens(3_000)
        live.check_request()  # the request goes out ...
        live.settle_model_tokens(3_000, 2_000)  # ... is cancelled, settled with an estimate
    live.record_cut("max_elapsed_seconds", "infra was still working", actor=INFRA)
    [stop] = lines
    assert (stop["check"], stop["at"], stop["cut"]) == (2, "request", True)
    assert "settled" not in stop  # the provider settles again in the replay, from its own log

    replay = replaying(budget, lines)
    with acting(INFRA):
        replay.reserve_model_tokens(3_000)
        with pytest.raises(ReplayedCut):
            replay.check_request()


def test_a_cut_right_after_a_reservation_counts_what_the_original_settled() -> None:
    """Cancelled before its request went out (waiting for another agent's readiness check):
    the provider settled it with its prompt's estimate. The replay raises the cut at that
    reservation and counts the same tokens, so the run's model-token totals match."""
    budget = RunBudget()
    live, lines = recorded(budget)
    with acting(INFRA):
        live.reserve_model_tokens(3_000)
        live.settle_model_tokens(3_000, 1_976)  # the provider's estimate for the cut request
    live.record_cut("time_share", "timeout after 1s", actor=INFRA, seconds=1.0)
    assert lines[0]["settled"] == 1_976

    replay = replaying(budget, lines)
    with acting(INFRA), pytest.raises(ReplayedCut):
        replay.reserve_model_tokens(3_000)
    assert (replay.model_tokens, replay.model_reserved) == (live.model_tokens, 0) == (1_976, 0)


def test_a_stop_before_any_check_of_a_turn_has_a_place_in_every_round() -> None:
    """Telemetry ran in round 1; in round 2 the orchestrator found no time left to start it.
    Each turn start is a check of its own, so the replay refuses round 2's start, not a
    check of round 1."""
    budget = RunBudget()
    live, lines = recorded(budget)
    live.begin_turn(TELEMETRY)
    with acting(TELEMETRY):
        live.charge_step()
    live.begin_turn(TELEMETRY)  # round 2
    detail = "no time was left on the run to start telemetry_investigator"
    live.record_refusal("max_elapsed_seconds", detail, actor=TELEMETRY)
    assert [(s["check"], s["at"], s["cut"]) for s in lines] == [(3, "turn", False)]

    replay = replaying(budget, lines)
    replay.begin_turn(TELEMETRY)
    with acting(TELEMETRY):
        replay.charge_step()
    with pytest.raises(BudgetExhausted, match="no time was left"):
        replay.begin_turn(TELEMETRY)


def test_a_replay_that_no_longer_makes_the_recorded_checks_fails_loudly() -> None:
    """A stop must never be raised at a different kind of check than it was recorded at."""
    stop = {"actor": TELEMETRY, "check": 2, "at": "tool_call", "kind": "max_tool_calls"}
    replay = replaying(RunBudget(), [{**stop, "detail": "2/2 tool calls used", "cut": False}])
    with acting(TELEMETRY):
        replay.charge_step()
        with pytest.raises(ReplayMismatch, match="record the run again"):
            replay.charge_step()  # this run's second check is a step, not a tool call


def test_a_run_without_a_recording_refuses_as_before_and_writes_nothing() -> None:
    tracker = BudgetTracker(RunBudget(max_steps=1))
    tracker.charge_step()
    with pytest.raises(BudgetExhausted, match="max_steps"):
        tracker.charge_step()
    tracker.record_cut("time_share", "timeout after 1s", actor=TELEMETRY)  # nowhere to write
    assert not tracker.replaying and tracker.steps == 1


def test_a_shared_allowance_is_refused_where_the_recording_says_and_stops_nothing() -> None:
    """The runbook searches of a run are shared too (one here): live, infrastructure took the
    last one and telemetry's search was refused. In the replay telemetry asks first, and is
    still the one refused; nothing raises, the caller reports a failed tool call."""
    limit, used = 1, 0
    live, lines = recorded(RunBudget())
    with acting(INFRA):
        assert live.allow("agent_search", used < limit, "used up")
        used += 1
    with acting(TELEMETRY):
        assert not live.allow("agent_search", used < limit, "used up")
    assert [(s["actor"], s["check"], s["allowance"]) for s in lines] == [(TELEMETRY, 1, True)]

    used = 0
    replay = replaying(RunBudget(), lines)
    with acting(TELEMETRY):
        assert not replay.allow("agent_search", used < limit, "used up")
    with acting(INFRA):
        assert replay.allow("agent_search", used < limit, "used up")
