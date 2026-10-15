"""Tests for evidence summaries: the structured text a provider reads (audit finding 2.1).

The summary of a tool result must keep the facts that explain an incident (the newest
errors, when errors began, metric extremes and the latest value) inside a few hundred
characters, the same way every time, without knowing anything about the simulator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

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


def test_the_same_evidence_always_gives_the_same_summary() -> None:
    item = evidence("get_service_logs", long_log())

    assert render_for_agents(item, ALERT) == render_for_agents(item, ALERT)


def test_without_an_alert_time_there_are_no_offsets() -> None:
    text = render_for_agents(evidence("get_service_logs", long_log()))

    assert "before the alert" not in text
    assert "NEWEST-MARKER" in text
