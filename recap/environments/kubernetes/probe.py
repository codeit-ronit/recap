"""The oracle's own HTTP probe, the alert rule reset waits for, and a scenario's HTTP burst.

Why a probe of its own and not Prometheus: the agents read Prometheus through
get_service_metrics, so the recovery oracle needs a channel they cannot reach (ADR 0010).
It asks each service directly through its loopback NodePort, like a user would: a fixed
number of requests, each with a short timeout; a failure is a refused connection, a timeout
or any answer outside 2xx/3xx. The oracle probes in two rounds a few seconds apart and only
a service healthy in both counts as healthy, so a pod that falls over seconds after turning
ready is not counted as recovered (docs/m5-integration-plan.md, 3.7).

The alert rule turns the same measurements into an alert: a service's probe error rate at
or above the scenario's threshold, replicas not ready, a crash-looping container or a stuck
rollout. The alert carries only what was measured, worded so the router's symptom patterns
can read it ("error", "readiness", "crash"), never the scenario's name or its answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from pydantic import Field

from recap.core.models import Contract
from recap.core.observations import GroundTruthHealth


@dataclass(frozen=True)
class ProbeResult:
    """One service's probe round: how many requests failed, and the first reason why."""

    service: str
    requests: int
    failures: int
    reason: str = ""

    @property
    def error_rate(self) -> float:
        return self.failures / self.requests if self.requests else 1.0


def http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """A client for loopback endpoints only: no proxy from the environment, no redirects."""
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)


async def probe_service(
    client: httpx.AsyncClient, service: str, url: str, requests: int, timeout: float
) -> ProbeResult:
    """Send `requests` GETs at once, each bounded by `timeout`; count what failed."""

    async def one() -> str:
        try:
            response = await asyncio.wait_for(client.get(url, timeout=timeout), timeout + 0.5)
        except (httpx.HTTPError, TimeoutError) as exc:
            return type(exc).__name__
        return "" if 200 <= response.status_code < 400 else f"HTTP {response.status_code}"

    outcomes = await asyncio.gather(*(one() for _ in range(requests)))
    failed = [outcome for outcome in outcomes if outcome]
    return ProbeResult(service, requests, len(failed), failed[0] if failed else "")


async def probe_round(
    urls: Mapping[str, str],
    requests: int,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, ProbeResult]:
    """Probe every service at once; a round takes about one request timeout at most."""
    async with http_client(transport) as client:
        results = await asyncio.gather(
            *(probe_service(client, s, url, requests, timeout) for s, url in urls.items())
        )
    return {result.service: result for result in results}


async def burst(
    url: str,
    requests: int,
    concurrency: int,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """A scenario's fault step: `requests` GETs, `concurrency` at a time. Returns how many
    were answered (any status): the step only has to have happened, not to succeed."""
    gate = asyncio.Semaphore(concurrency)
    async with http_client(transport) as client:

        async def one() -> bool:
            async with gate:
                try:
                    await asyncio.wait_for(client.get(url, timeout=timeout), timeout + 0.5)
                except (httpx.HTTPError, TimeoutError):
                    return False
                return True

        answered = await asyncio.gather(*(one() for _ in range(requests)))
    return sum(answered)


# --- the oracle: two rounds ------------------------------------------------------------------


def combine_rounds(
    first: Mapping[str, GroundTruthHealth], second: Mapping[str, GroundTruthHealth]
) -> dict[str, GroundTruthHealth]:
    """Healthy only if healthy in both rounds: the worse status, fewer ready replicas and
    the higher error rate of the two."""
    combined: dict[str, GroundTruthHealth] = {}
    for service, later in second.items():
        earlier = first.get(service, later)
        status = later.status if later.status != "Running" else earlier.status
        combined[service] = GroundTruthHealth(
            service=service,
            status=status,
            ready_replicas=min(earlier.ready_replicas, later.ready_replicas),
            replicas=later.replicas,
            error_rate=max(earlier.error_rate, later.error_rate),
        )
    return combined


# --- the alert rule -------------------------------------------------------------------------


class AlertRule(Contract):
    """When reset considers the scenario's fault visible: what the alert fires on."""

    service: str  # the service the alert names first (the one it watches)
    error_rate: float = Field(0.05, gt=0, le=1)  # a probe error rate at or above this fires
    not_ready: bool = True  # fewer ready replicas than desired fires
    crash_looping: bool = True  # a container in CrashLoopBackOff, or a stuck rollout, fires
    rounds: int = Field(2, ge=1, le=10)  # consecutive rounds that must fire
    timeout_seconds: float = Field(180.0, gt=0, le=1800)


@dataclass(frozen=True)
class Condition:
    """What the API says about one service at one moment (for the alert rule)."""

    ready: int
    replicas: int
    crash_looping: bool
    rollout_stuck: bool


def alert_symptoms(
    rule: AlertRule, probes: Mapping[str, ProbeResult], conditions: Mapping[str, Condition]
) -> dict[str, list[str]]:
    """Service -> the measured symptoms that fire the rule ({} while nothing fires)."""
    fired: dict[str, list[str]] = {}
    for service, condition in conditions.items():
        symptoms: list[str] = []
        probe = probes.get(service)
        if probe is not None and probe.error_rate >= rule.error_rate:
            symptoms.append(
                f"{service}: HTTP error rate {probe.error_rate:.2f} over {probe.requests} probe "
                f"requests (first failure: {probe.reason or 'unknown'})"
            )
        if rule.not_ready and condition.ready < condition.replicas:
            symptoms.append(
                f"{service}: {condition.ready} of {condition.replicas} replicas ready "
                "(readiness failing)"
            )
        if rule.crash_looping and condition.crash_looping:
            symptoms.append(f"{service}: a container is in CrashLoopBackOff (crash loop)")
        if rule.crash_looping and condition.rollout_stuck:
            symptoms.append(f"{service}: its rollout exceeded the progress deadline (unavailable)")
        if symptoms:
            fired[service] = symptoms
    return fired
