"""Context engineering under pressure: a real model-token budget, errors that survive a cut,
and evidence that must be kept being shortened instead of dropped (M3, step 5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from recap.context.evidence import EvidenceRegistry
from recap.context.packer import ContextPacker
from recap.core.config import PolicyLimits
from recap.core.errors import BudgetExhausted
from recap.core.models import Incident, RunBudget, SourceType
from recap.harness.budget import BudgetTracker
from recap.tools.gateway import _bound

T0 = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)


# --- the model-token budget -------------------------------------------------------------------


def test_model_tokens_are_reserved_first_and_settled_with_the_real_count() -> None:
    budget = BudgetTracker(RunBudget(max_model_tokens=10_000))
    first = budget.reserve_model_tokens(4_000)
    second = budget.reserve_model_tokens(4_000)  # a parallel agent's request in flight
    with pytest.raises(BudgetExhausted) as stop:
        budget.reserve_model_tokens(4_000)  # both in flight: a third would overshoot
    assert stop.value.limit == "max_model_tokens"
    budget.settle_model_tokens(first, 2_500)  # the server reported fewer than reserved
    budget.settle_model_tokens(second, 3_100)
    assert (budget.model_tokens, budget.model_reserved) == (5_600, 0)
    assert budget.reserve_model_tokens(4_000) == 4_000  # room again: 5,600 + 4,000 <= 10,000
    snapshot = budget.snapshot()
    assert (snapshot["model_tokens"], snapshot["max_model_tokens"]) == (5_600, 10_000)


# --- errors survive a cut of a long log list -------------------------------------------------


def log_lines(count: int, errors_at: tuple[int, ...]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": (T0 + timedelta(seconds=i)).isoformat(),
            "level": "ERROR" if i in errors_at else "INFO",
            "service": "checkout",
            "message": f"ERROR {i} (version=v2.4.1)" if i in errors_at else f"GET /items {i} 200",
        }
        for i in range(count)
    ]


def test_a_cut_log_list_keeps_its_errors_in_time_order() -> None:
    lines = log_lines(200, errors_at=(5, 6, 7))  # errors early, then 192 lines of INFO noise
    data, cut = _bound({"lines": lines}, PolicyLimits(max_list_items=60))
    kept = data["lines"]
    assert cut and len(kept) == 60
    assert [line["message"] for line in kept if line["level"] == "ERROR"] == [
        "ERROR 5 (version=v2.4.1)",
        "ERROR 6 (version=v2.4.1)",
        "ERROR 7 (version=v2.4.1)",
    ]
    stamps = [line["timestamp"] for line in kept]
    assert stamps == sorted(stamps)  # still chronological
    assert kept[-1] == lines[-1]  # and the newest line is still there
    # Before M3 the cut kept only the newest 60 lines: every error was lost.
    assert all(line["level"] == "INFO" for line in lines[-60:])


def test_many_errors_leave_room_for_the_newest_lines() -> None:
    lines = log_lines(200, errors_at=tuple(range(0, 200, 2)))  # every other line an error
    kept = _bound({"lines": lines}, PolicyLimits(max_list_items=60))[0]["lines"]
    assert kept[0] == lines[0]  # the onset: the first error
    assert lines[-1] in kept and lines[-2] in kept  # the newest lines, whatever their level
    assert len(kept) == 60


def test_other_lists_are_cut_as_before() -> None:
    points = [{"timestamp": str(i), "value": float(i)} for i in range(100)]
    data, _ = _bound(
        {"points": points, "events": list(range(100))}, PolicyLimits(max_list_items=10)
    )
    assert data["points"] == points[-10:]  # chronological: the newest
    assert data["events"] == list(range(10))  # newest first: the first ones


# --- must-keep evidence is shortened, not dropped -------------------------------------------


def incident() -> Incident:
    return Incident(
        incident_id="inc-1",
        scenario_id="s",
        service="checkout",
        symptoms=["checkout: HTTP 5xx ratio above 30%"],
        created_at=T0,
        environment="simulated",
        budget=RunBudget(),
    )


def evidence(registry: EvidenceRegistry, service: str, words: int) -> str:
    text = f"{service} checkout HTTP 5xx " + " ".join(f"word{i}" for i in range(words))
    return registry.add(
        source_type=SourceType.TOOL,
        source_ref=f"mcp:get_service_logs(service={service})",
        content=text,
        tool_name="unknown_tool",  # no summary: the text is packed as it is
    ).id


def test_must_keep_evidence_beyond_the_budget_is_shortened_not_dropped() -> None:
    """Four contradictions of about 400 tokens each, room for three and a bit."""
    registry = EvidenceRegistry("run-abc123def4", "inc-1")
    against = [evidence(registry, f"svc{i}", 300) for i in range(4)]
    other = evidence(registry, "cart", 300)
    packer = ContextPacker(max_item_tokens=400)
    budget = 3 * 400 + 150

    def pack(priority: list[str]) -> Any:
        return packer.build(
            incident=incident().agent_view(),
            evidence=registry.all(),
            max_tokens=budget,
            priority_ids=priority,
        )

    kept = {item.item_id: item for item in pack(against).items}
    assert set(against) <= set(kept)  # every contradiction is still in the pack ...
    shortened = [
        i
        for i in against
        if "compacted; full record" in kept[i].text and kept[i].token_estimate < 300
    ]
    assert len(shortened) == 1  # ... one of them shortened into the room left
    assert other not in kept  # a lower-ranked item never gets that
    assert pack(against).tokens_after <= budget
    # Without the must-keep rule (no priority), the same budget simply drops items.
    assert len(pack([]).dropped_ids) == 2
