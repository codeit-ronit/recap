"""Metrics from the demo's own Prometheus: fixed query templates, read-only HTTP calls.

A metric name from RECAP's fixed list picks one PromQL template; the template's only
parameters are the namespace and the service, both validated Kubernetes names, and the rate
window, a validated duration. Nothing a model writes ever becomes query text.

    error_rate      5xx share of the service's requests (health and metrics endpoints left
                    out), over the rate window
    latency_p95_ms  95th percentile of the request duration histogram, in milliseconds
    memory_mb       the largest resident memory of the service's processes (process RSS, not
                    the container's working set); the series carries the container limit
    cpu_pct         the busiest process's CPU seconds per second, against the CPU limit
    restarts        not from Prometheus: the pods' restart counts (see the engine)

There is never a made-up number. An empty answer (no traffic, no series), NaN or infinity
(histogram_quantile without requests, 0/0) is a MetricsUnavailable error, which the gateway
records as a failed tool call; the adapter refuses to start when Prometheus is unreachable.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from recap.core.observations import MetricName, MetricPoint

# Request metrics leave out the kubelet's probes and Prometheus' own scrapes.
_NOT_PROBES = 'handler!~"/?(healthz|readyz|metrics)"'
_TEMPLATES: dict[str, str] = {
    "error_rate": (
        '(sum(rate(http_requests_total{{{requests},code=~"5.."}}[{window}])) or vector(0))'
        " / sum(rate(http_requests_total{{{requests}}}[{window}]))"
    ),
    "latency_p95_ms": (
        "histogram_quantile(0.95, sum by (le) "
        "(rate(http_request_duration_seconds_bucket{{{requests}}}[{window}]))) * 1000"
    ),
    "memory_mb": "max(process_resident_memory_bytes{{{process}}}) / 1048576",
    "cpu_pct": "max(rate(process_cpu_seconds_total{{{process}}}[{window}]))",
}
PROMETHEUS_METRICS = frozenset(_TEMPLATES)
UNITS: dict[str, str] = {
    "error_rate": "ratio",
    "latency_p95_ms": "ms",
    "memory_mb": "MB (process RSS, the largest of the service's pods)",
    "cpu_pct": "percent of the container CPU limit",
    "restarts": "count (current total over the service's pods; the API keeps no history)",
}


class MetricsUnavailable(RuntimeError):
    """No number exists for this question. A tool error, never a made-up value."""


def promql(metric: str, namespace: str, service: str, window: str) -> str:
    """The fixed query for one metric of one service (the fake cluster answers the same)."""
    template = _TEMPLATES.get(metric)
    if template is None:
        raise ValueError(f"unknown metric '{metric}'")
    process = f'namespace="{namespace}",app="{service}"'
    return template.format(process=process, requests=f"{process},{_NOT_PROBES}", window=window)


class PrometheusClient:
    """Read-only Prometheus HTTP API client on the loopback interface."""

    def __init__(
        self,
        base_url: str,
        namespace: str,
        rate_window: str,
        *,
        timeout_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.namespace = namespace
        self.rate_window = rate_window
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def query(self, metric: MetricName | str, service: str) -> str:
        return promql(metric, self.namespace, service, self.rate_window)

    async def range(
        self, metric: MetricName, service: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[MetricPoint]:
        """One point per step from start to end; MetricsUnavailable if any is missing or bad."""
        params = {
            "query": self.query(metric, service),
            "start": f"{start.timestamp():.3f}",
            "end": f"{end.timestamp():.3f}",
            "step": str(step_seconds),
        }
        data = await self._get("/api/v1/query_range", params, metric, service)
        series = _single(data, "matrix", metric, service)
        values = series.get("values") if series else None
        if not isinstance(values, list) or not values:
            raise MetricsUnavailable(_no_data(metric, service))
        return [_point(value, metric, service) for value in values]

    async def instant(self, metric: MetricName, service: str, at: datetime) -> float:
        """The metric's value at one moment; MetricsUnavailable if Prometheus has none."""
        params = {"query": self.query(metric, service), "time": f"{at.timestamp():.3f}"}
        data = await self._get("/api/v1/query", params, metric, service)
        sample = _single(data, "vector", metric, service)
        value = sample.get("value") if sample else None
        if value is None:
            raise MetricsUnavailable(_no_data(metric, service))
        return _point(value, metric, service).value

    async def server_time(self) -> datetime:
        """Prometheus' own clock (it runs inside the cluster's VM): for the skew check."""
        data = await self._get("/api/v1/query", {"query": "time()"}, "time", "prometheus")
        if data.get("resultType") != "scalar" or not isinstance(data.get("result"), list):
            raise MetricsUnavailable("Prometheus answered time() with an unexpected shape")
        point = _point(data["result"], "time", "prometheus")
        return datetime.fromtimestamp(point.value, UTC)

    async def _get(
        self, path: str, params: Mapping[str, str], metric: str, service: str
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
                trust_env=False,  # never through a proxy: the server is on this machine
                follow_redirects=False,
            ) as client:
                response = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise MetricsUnavailable(
                f"Prometheus could not be reached for {metric} of {service} ({type(exc).__name__})"
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            raise MetricsUnavailable(
                f"Prometheus refused the {metric} query for {service} (HTTP {response.status_code})"
            )
        if body.get("status") != "success" or not isinstance(body.get("data"), dict):
            raise MetricsUnavailable(
                f"Prometheus could not answer the {metric} query for {service}"
            )
        return body["data"]


def _single(data: Mapping[str, Any], kind: str, metric: str, service: str) -> dict[str, Any]:
    """The one series an aggregated query returns ({} when there is none)."""
    result = data.get("result")
    if data.get("resultType") != kind or not isinstance(result, list):
        raise MetricsUnavailable(f"Prometheus answered the {metric} query with an unexpected shape")
    if len(result) > 1 or (result and not isinstance(result[0], dict)):
        raise MetricsUnavailable(f"Prometheus returned several series for {metric} of {service}")
    return result[0] if result else {}


def _point(raw: Any, metric: str, service: str) -> MetricPoint:
    """[unix time, "value"] -> a point; NaN and infinity are refused, never passed on."""
    if not isinstance(raw, list) or len(raw) != 2:
        raise MetricsUnavailable(f"Prometheus returned a malformed sample for {metric}")
    try:
        stamp, value = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        raise MetricsUnavailable(f"Prometheus returned a malformed sample for {metric}") from None
    if not (math.isfinite(stamp) and math.isfinite(value)):
        raise MetricsUnavailable(
            f"{metric} of {service} has no finite value at "
            f"{datetime.fromtimestamp(stamp, UTC).isoformat() if math.isfinite(stamp) else '?'} "
            "(no requests to measure, or no samples): no number is reported"
        )
    return MetricPoint(timestamp=datetime.fromtimestamp(stamp, UTC), value=value)


def _no_data(metric: str, service: str) -> str:
    return (
        f"Prometheus has no {metric} data for {service} in that window "
        "(no traffic, no running process, or the series is not scraped): no number is reported"
    )
