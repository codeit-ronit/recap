"""Prometheus queries and the oracle's probe (recap/environments/kubernetes/metrics.py, probe.py).

The real HTTP code runs; only the network is an httpx.MockTransport (no Prometheus, no
cluster). Metrics come from fixed PromQL templates whose only parameters are validated
names, and a number that does not exist (no data, NaN, infinity) is an error, never made up.
The probe counts what a user would see; the alert rule fires on measurements only.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from recap.core.observations import GroundTruthHealth
from recap.environments.kubernetes import probe
from recap.environments.kubernetes.metrics import MetricsUnavailable, PrometheusClient, promql

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def test_queries_are_fixed_templates_of_validated_names() -> None:
    assert promql("error_rate", "recap-demo", "backend", "1m") == (
        '(sum(rate(http_requests_total{namespace="recap-demo",app="backend",'
        'handler!~"/?(healthz|readyz|metrics)",code=~"5.."}[1m])) or vector(0))'
        ' / sum(rate(http_requests_total{namespace="recap-demo",app="backend",'
        'handler!~"/?(healthz|readyz|metrics)"}[1m]))'
    )
    assert promql("memory_mb", "recap-demo", "frontend", "1m") == (
        'max(process_resident_memory_bytes{namespace="recap-demo",app="frontend"}) / 1048576'
    )
    assert "histogram_quantile(0.95" in promql("latency_p95_ms", "recap-demo", "backend", "90s")
    assert "[90s]" in promql("cpu_pct", "recap-demo", "backend", "90s")
    with pytest.raises(ValueError, match="unknown metric"):
        promql("delete_namespace", "recap-demo", "backend", "1m")
    with pytest.raises(ValueError, match="unknown metric"):
        promql("restarts", "recap-demo", "backend", "1m")  # read from the API, not Prometheus


class FakePrometheus:
    """Answers query_range/query with whatever `values` says, and records the requests."""

    def __init__(self, values: list[Any] | None = None, **answer: Any) -> None:
        self.values = values
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "status" in self.answer or "code" in self.answer:
            body = {"status": self.answer.get("status", "success"), "data": {}}
            return httpx.Response(self.answer.get("code", 200), json=body)
        if self.values is None:
            result: list[Any] = []
        elif request.url.path.endswith("query_range"):
            result = [{"metric": {}, "values": self.values}]
        else:
            result = [{"metric": {}, "value": self.values[-1]}]
        kind = "matrix" if request.url.path.endswith("query_range") else "vector"
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": kind, "result": result}}
        )

    def client(self) -> PrometheusClient:
        return PrometheusClient(
            "http://127.0.0.1:30090", "recap-demo", "1m", transport=httpx.MockTransport(self.handle)
        )


async def test_a_range_query_returns_one_point_per_step_in_utc() -> None:
    stamps = [T0.timestamp() + 60 * i for i in range(3)]
    fake = FakePrometheus([[stamps[0], "0"], [stamps[1], "0.01"], [stamps[2], "0.42"]])

    points = await fake.client().range("error_rate", "backend", T0, T0 + timedelta(minutes=2), 60)

    assert [p.value for p in points] == [0.0, 0.01, 0.42]
    assert points[-1].timestamp == T0 + timedelta(minutes=2)
    [request] = fake.requests
    assert request.url.path == "/api/v1/query_range"
    assert request.url.params["query"] == promql("error_rate", "recap-demo", "backend", "1m")
    assert request.url.params["step"] == "60"


@pytest.mark.parametrize(
    ("fake", "reason"),
    [
        (FakePrometheus(None), "no error_rate data"),
        (FakePrometheus([[T0.timestamp(), "NaN"]]), "no finite value"),
        (FakePrometheus([[T0.timestamp(), "+Inf"]]), "no finite value"),
        (FakePrometheus([[T0.timestamp(), "lots"]]), "malformed sample"),
        (FakePrometheus(status="error"), "could not answer"),
        (FakePrometheus(code=400), "HTTP 400"),
    ],
    ids=["no-data", "nan", "inf", "garbage", "status-error", "http-400"],
)
async def test_a_number_that_does_not_exist_is_an_error_never_made_up(
    fake: FakePrometheus, reason: str
) -> None:
    with pytest.raises(MetricsUnavailable, match=reason):
        await fake.client().range("error_rate", "backend", T0, T0, 60)


async def test_several_series_for_an_aggregate_are_refused() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        series = [{"metric": {"pod": n}, "value": [T0.timestamp(), "1"]} for n in ("a", "b")]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": series}}
        )

    client = PrometheusClient(
        "http://127.0.0.1:30090", "recap-demo", "1m", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(MetricsUnavailable, match="several series"):
        await client.instant("memory_mb", "backend", T0)


async def test_an_unreachable_prometheus_is_an_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = PrometheusClient(
        "http://127.0.0.1:30090", "recap-demo", "1m", transport=httpx.MockTransport(refuse)
    )
    with pytest.raises(MetricsUnavailable, match="could not be reached"):
        await client.instant("latency_p95_ms", "frontend", T0)


async def test_prometheus_clock_for_the_skew_check() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == "time()"
        data = {"resultType": "scalar", "result": [T0.timestamp(), f"{T0.timestamp():.3f}"]}
        return httpx.Response(200, json={"status": "success", "data": data})

    client = PrometheusClient(
        "http://127.0.0.1:30090", "recap-demo", "1m", transport=httpx.MockTransport(handle)
    )
    assert await client.server_time() == T0


# --- the oracle's probe ------------------------------------------------------------------------


def service_mock(behaviour: dict[str, Any]) -> httpx.MockTransport:
    """frontend answers with the next status in its list; backend refuses connections."""
    counters: dict[str, int] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host + ":" + str(request.url.port)
        answer = behaviour[host]
        if answer == "refuse":
            raise httpx.ConnectError("connection refused", request=request)
        index = counters.get(host, 0)
        counters[host] = index + 1
        return httpx.Response(answer[index % len(answer)])

    return httpx.MockTransport(handle)


async def test_a_probe_round_counts_failures_like_a_user_sees_them() -> None:
    transport = service_mock(
        {"127.0.0.1:30080": [200, 500, 200, 503, 200], "127.0.0.1:30081": "refuse"}
    )
    urls = {"frontend": "http://127.0.0.1:30080/", "backend": "http://127.0.0.1:30081/items"}

    results = await probe.probe_round(urls, 10, 1.0, transport)

    assert results["frontend"].failures == 4 and results["frontend"].error_rate == 0.4
    assert results["frontend"].reason.startswith("HTTP 5")
    assert results["backend"].error_rate == 1.0 and results["backend"].reason == "ConnectError"


async def test_a_burst_reports_how_many_requests_were_answered() -> None:
    transport = service_mock({"127.0.0.1:30081": [200, 503]})
    assert await probe.burst("http://127.0.0.1:30081/report", 9, 3, 1.0, transport) == 9


def test_healthy_means_healthy_in_both_rounds() -> None:
    def health(status: str, ready: int, error_rate: float) -> GroundTruthHealth:
        return GroundTruthHealth(
            service="backend",
            status=status,
            ready_replicas=ready,
            replicas=2,
            error_rate=error_rate,
        )

    first = {"backend": health("Running", 2, 0.0)}
    flapping = {"backend": health("CrashLoopBackOff", 1, 0.3)}
    assert probe.combine_rounds(first, flapping)["backend"] == health("CrashLoopBackOff", 1, 0.3)
    assert probe.combine_rounds(flapping, first)["backend"] == health("CrashLoopBackOff", 1, 0.3)
    assert probe.combine_rounds(first, first)["backend"] == health("Running", 2, 0.0)


# --- the alert rule ----------------------------------------------------------------------------


def test_the_alert_fires_on_measurements_worded_for_the_router() -> None:
    rule = probe.AlertRule(service="frontend")
    probes = {
        "frontend": probe.ProbeResult("frontend", 10, 4, "HTTP 500"),
        "backend": probe.ProbeResult("backend", 10, 0),
    }
    calm = probe.Condition(ready=2, replicas=2, crash_looping=False, rollout_stuck=False)
    stuck = probe.Condition(ready=1, replicas=2, crash_looping=True, rollout_stuck=True)

    fired = probe.alert_symptoms(rule, probes, {"frontend": calm, "backend": stuck})

    assert set(fired) == {"frontend", "backend"}
    assert fired["frontend"] == [
        "frontend: HTTP error rate 0.40 over 10 probe requests (first failure: HTTP 500)"
    ]
    assert len(fired["backend"]) == 3
    words = " ".join(s for symptoms in fired.values() for s in symptoms)
    # The router's symptom families (recap/routing/signals.py): errors, readiness, crashes.
    for family in (r"5xx|error|failure", r"readiness|unavailable|down|crash"):
        assert re.search(family, words)
    assert (
        probe.alert_symptoms(rule, {**probes, "frontend": probes["backend"]}, {"frontend": calm})
        == {}
    )


def test_a_scenario_can_switch_rules_off() -> None:
    rule = probe.AlertRule(service="backend", not_ready=False, crash_looping=False, error_rate=0.5)
    probes = {"backend": probe.ProbeResult("backend", 10, 4)}
    stuck = probe.Condition(ready=0, replicas=2, crash_looping=True, rollout_stuck=True)
    assert probe.alert_symptoms(rule, probes, {"backend": stuck}) == {}
    assert json.loads(rule.model_dump_json())["rounds"] == 2
