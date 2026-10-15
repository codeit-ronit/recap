"""Tests for evidence summaries: the structured text a provider reads (audit finding 2.1).

The summary of a tool result must keep the facts that explain an incident (the newest
errors, when errors began, metric extremes and the latest value) inside a few hundred
characters, the same way every time, without knowing anything about the simulator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from recap.context import summaries
from recap.context.evidence import EvidenceRegistry
from recap.context.summaries import render_for_agents
from recap.core.models import Evidence, SourceType
from recap.core.observations import (
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)

ALERT = datetime(2026, 3, 14, 10, 0, tzinfo=UTC)


def evidence(tool: str, observation: BaseModel | dict[str, Any], content: str = "") -> Evidence:
    """Store an observation the way the gateway does: structured data plus raw text."""
    registry = EvidenceRegistry("run-sum0000001", "inc-1")
    data = observation if isinstance(observation, dict) else observation.model_dump(mode="json")
    return registry.add(
        source_type=SourceType.TOOL,
        source_ref=f"mcp:{tool}()",
        content=content or str(data),
        data=data,
        tool_name=tool,
    )


def log(minutes_before: int, level: str, message: str) -> LogLine:
    return LogLine(
        timestamp=ALERT - timedelta(minutes=minutes_before),
        level=level,  # type: ignore[arg-type]
        service="checkout",
        message=message,
    )


def long_log() -> LogQueryResult:
    """60 INFO lines, then 5 ERROR lines; the newest ERROR line carries a marker."""
    lines = [log(64 - i, "INFO", f"GET /api/orders 200 in {80 + i}ms") for i in range(60)]
    lines += [
        log(4 - i, "ERROR", "PricingClient call failed: timeout after 3000ms") for i in range(4)
    ]
    lines.append(log(0, "ERROR", "PricingClient call failed: NEWEST-MARKER"))
    return LogQueryResult(service="checkout", lines=lines, total_matched=65)


# --- logs -------------------------------------------------------------------------------------


def test_the_newest_error_at_the_end_of_a_long_log_is_in_the_summary() -> None:
    text = render_for_agents(evidence("get_service_logs", long_log()), ALERT)

    assert "NEWEST-MARKER" in text
    assert "levels: 5 ERROR, 60 INFO" in text
    assert "first ERROR: 09:56 (4 min before the alert)" in text
    assert text.endswith(f"raw evidence: {evidence('get_service_logs', long_log()).id}")


def test_repeated_log_messages_are_collapsed_with_a_count() -> None:
    text = render_for_agents(evidence("get_service_logs", long_log()), ALERT)

    assert "09:56-09:59 ×4 ERROR PricingClient call failed: timeout after 3000ms" in text
    # The 60 INFO lines differ only in numbers, so they count as one message shape.
    assert "60× INFO GET /api/orders # in #ms" in text


def test_the_newest_error_is_kept_however_many_warnings_came_after_it() -> None:
    """Three ERRORs, then 30 newer distinct WARN lines: the newest-lines list fills with
    warnings, but the summary still names the newest ERROR on its own line."""
    lines = [log(45, "ERROR", "first failure"), log(44, "ERROR", "second failure")]
    lines.append(log(43, "ERROR", "NEWEST-ERROR-MARKER"))
    lines += [log(30 - i, "WARN", f"slow request number {i:02d} " + "y" * 40) for i in range(30)]
    result = LogQueryResult(service="checkout", lines=lines, total_matched=33)

    text = render_for_agents(evidence("get_service_logs", result), ALERT, max_chars=900)

    assert len(text) <= 900
    assert "newest ERROR: 09:17 (43 min before the alert): NEWEST-ERROR-MARKER" in text
    assert "first ERROR: 09:15 (45 min before the alert): first failure" in text


def test_a_tight_budget_shows_fewer_lines_but_keeps_the_newest() -> None:
    lines = [
        log(40 - i, "ERROR", f"distinct failure number {i:02d} " + "x" * 60) for i in range(40)
    ]
    result = LogQueryResult(service="checkout", lines=lines, total_matched=40)
    item = evidence("get_service_logs", result)

    roomy = render_for_agents(item, ALERT)
    tight = render_for_agents(item, ALERT, max_chars=900)

    assert len(tight) <= 900 < len(roomy)
    assert "distinct failure number 39" in tight  # the newest line survives
    assert "distinct failure number 30" not in tight  # older ones make room


# --- metrics ----------------------------------------------------------------------------------


def test_a_metric_summary_keeps_baseline_extremes_and_the_latest_value() -> None:
    values = [80.0] * 46 + [1250.0, 3170.0, 2905.0]
    points = [
        MetricPoint(timestamp=ALERT - timedelta(minutes=5 * (48 - k)), value=v)
        for k, v in enumerate(values)
    ]
    series = MetricSeries(service="checkout", metric="latency_p95_ms", unit="ms", points=points)

    text = render_for_agents(evidence("get_service_metrics", series), ALERT)

    assert "49 points every 5 min, 06:00 to 10:00" in text
    assert "first 80; latest 2905 at 10:00 (change +2825)" in text
    assert "min 80 at 06:00; max 3170 at 09:55" in text
    assert "largest jump: +1920 from 09:50 to 09:55 (5 min before the alert)" in text


def test_points_that_are_not_finite_numbers_are_left_out_of_a_metric_summary() -> None:
    """Tool data is untrusted: NaN and infinity must not poison min, max, jump or latest."""
    values = [80.0, float("nan"), 95.0, float("inf"), 90.0, float("-inf")]
    points = [
        MetricPoint(timestamp=ALERT - timedelta(minutes=5 * (5 - k)), value=v)
        for k, v in enumerate(values)
    ]
    series = MetricSeries(service="checkout", metric="latency_p95_ms", unit="ms", points=points)
    item = evidence("get_service_metrics", series)

    text = render_for_agents(item, ALERT)

    assert text.startswith("Metric latency_p95_ms of checkout (ms): 3 points every 5 min")
    assert "first 80; latest 90 at 09:55 (change +10)" in text
    assert "min 80 at 09:35; max 95 at 09:45" in text
    assert "3 points are not finite numbers (left out)" in text
    assert "nan" not in text and "inf" not in text


def test_a_metric_with_no_finite_points_says_so() -> None:
    nan = [MetricPoint(timestamp=ALERT, value=float("nan"))]
    series = MetricSeries(service="checkout", metric="error_rate", unit="ratio", points=nan)

    text = render_for_agents(evidence("get_service_metrics", series), ALERT)

    assert text.splitlines()[:2] == [
        "Metric error_rate of checkout (ratio): no finite data points.",
        "1 points are not finite numbers (left out)",
    ]


def test_a_metric_with_a_limit_reports_its_share_of_the_limit() -> None:
    points = [MetricPoint(timestamp=ALERT - timedelta(minutes=m), value=200.0) for m in (10, 5, 0)]
    series = MetricSeries(
        service="payment", metric="memory_mb", unit="MB", points=points, limit=512.0
    )

    text = render_for_agents(evidence("get_service_metrics", series), ALERT)

    assert "limit 512 MB: latest at 39% of it, max at 39%" in text
    assert "all points are equal" in text  # a flat series has no "jump" to report


# --- health, deployments, events --------------------------------------------------------------


def test_health_is_one_line() -> None:
    snapshot = HealthSnapshot(
        service="payment",
        status="Running",
        ready_replicas=2,
        replicas=2,
        error_rate=0.001,
        latency_p95_ms=95.0,
        observed_at=ALERT,
    )

    text = render_for_agents(evidence("get_service_health", snapshot), ALERT)

    assert text.splitlines()[0] == (
        "Health of payment at 10:00: Running, 2/2 replicas ready, error rate 0.001, "
        "p95 latency 95 ms"
    )


def test_deployments_are_newest_first_with_their_distance_from_the_alert() -> None:
    releases = DeploymentList(
        deployments=[
            DeploymentRecord(
                service="checkout",
                version="v2.4.1",
                previous_version="v2.4.0",
                deployed_at=ALERT - timedelta(minutes=12),
                change_summary="Price rounding fix",
            ),
            DeploymentRecord(
                service="checkout", version="v2.4.0", deployed_at=ALERT - timedelta(days=2)
            ),
        ]
    )

    lines = render_for_agents(evidence("get_recent_deployments", releases), ALERT).splitlines()

    assert lines[1] == (
        "- checkout v2.4.1 (previous v2.4.0), complete, 09:48 (12 min before the alert): "
        "Price rounding fix"
    )
    assert lines[2] == "- checkout v2.4.0, complete, 2026-03-12 10:00 (2 days before the alert)"


def test_events_are_grouped_with_counts_newest_group_first() -> None:
    def event(minutes_before: int, reason: str, count: int = 1) -> ClusterEvent:
        return ClusterEvent(
            timestamp=ALERT - timedelta(minutes=minutes_before),
            type="Warning",
            reason=reason,
            object="pod/payment-1",
            service="payment",
            message=f"{reason} for payment",
            count=count,
        )

    listing = EventList(
        events=[event(1, "BackOff", 3), event(2, "OOMKilled"), event(7, "OOMKilled")]
    )

    lines = render_for_agents(evidence("get_k8s_like_events", listing), ALERT).splitlines()

    assert lines[0] == "Cluster events: 5 events in 2 groups (newest first)"
    assert lines[1].startswith("- payment Warning BackOff ×3, latest 09:59 (1 min before")
    assert lines[2].startswith("- payment Warning OOMKilled ×2, latest 09:58 (2 min before")


# --- fallbacks and determinism ----------------------------------------------------------------


def test_unknown_tools_and_unexpected_data_are_shown_raw() -> None:
    unknown = evidence("some_future_tool", {"anything": 1}, content="raw text of the tool")
    misshaped = evidence("get_service_logs", {"lines": "not a list"}, content="raw log text")

    assert render_for_agents(unknown, ALERT) == "raw text of the tool"
    assert render_for_agents(misshaped, ALERT) == "raw log text"


def test_health_with_values_that_are_not_finite_is_still_one_line() -> None:
    """V1's number formatting crashed on NaN (int(nan)); untrusted data may contain it."""
    snapshot = HealthSnapshot(
        service="payment",
        status="Running",
        ready_replicas=2,
        replicas=2,
        error_rate=float("nan"),
        latency_p95_ms=float("inf"),
        observed_at=ALERT,
    )

    text = render_for_agents(evidence("get_service_health", snapshot), ALERT)

    assert text.splitlines()[0] == (
        "Health of payment at 10:00: Running, 2/2 replicas ready, error rate nan, "
        "p95 latency inf ms"
    )


def test_a_summary_that_cannot_be_built_falls_back_to_the_raw_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever odd value a summarizer trips over, the run goes on with the raw text."""

    def trips_over_a_value(*_: object) -> list[str]:
        raise OverflowError("date value out of range")

    monkeypatch.setitem(
        summaries._SUMMARIES, "get_service_logs", (LogQueryResult, trips_over_a_value)
    )
    item = evidence("get_service_logs", long_log(), content="the raw log text")

    assert render_for_agents(item, ALERT) == "the raw log text"


def test_the_same_observation_always_gives_the_same_summary() -> None:
    """Built twice from scratch (new registry, new objects): byte-for-byte the same summary."""
    first = evidence("get_service_logs", long_log())
    second = evidence("get_service_logs", long_log())
    assert first is not second

    text = render_for_agents(first, ALERT)

    assert text != first.content  # a real summary, not the raw fallback
    assert text.startswith("Logs of checkout: 65 lines returned")
    assert render_for_agents(second, ALERT) == text


def test_without_an_alert_time_there_are_no_offsets() -> None:
    item = evidence("get_service_logs", long_log())

    text = render_for_agents(item)

    assert text != item.content  # a real summary, not the raw fallback
    # With no alert to measure from, times are full dates instead of "N min before the alert".
    assert "first ERROR: 2026-03-14 09:56: PricingClient call failed: timeout after 3000ms" in text
    assert "before the alert" not in text and "after the alert" not in text
