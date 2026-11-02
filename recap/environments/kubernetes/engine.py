"""KubernetesEnvironment: the TargetEnvironment adapter for RECAP's local Kind cluster.

    controller (trusted)     reset, describe, bind_executor, health_check
    MCP server subprocess    observe_*, plan_rollback, execute_authorized
           |                          |
           +---- KubernetesEnvironment (kind "kind"), one per process, same run id
                    |  typed operations (kubectl.py), one identity per purpose:
                    |    recap-reader (reads)   recap-writer (the two writes)
                    |    recap-oracle (health)  recap-scenario (reset's fault steps)
                    |  fixed PromQL (metrics.py), the oracle's own probe (probe.py)
                    v
           Kind cluster recap-m5, namespace recap-demo (frontend, backend)

The rest of RECAP does not change: agents keep the same ten MCP tools, every call passes
the same gateway, every write the same approval and capability (ADR 0007, ADR 0014). This
class only translates (docs/m5-integration-plan.md, sections 3 and 4):

* reset: preflight (cluster pinning, version skew, Prometheus and its clock), the namespace
  lease, the baseline images restored and healthy, a calm baseline, the scenario's typed
  fault steps, then a bounded wait for the measured alert. Nothing about the fault leaves.
* reads: the agents' view as RECAP's observation models, limited to this run's window
  (revisions, events and logs from before the run's baseline belong to earlier runs).
* the single write path: rollback or restart only, the write switch, the capability
  verified and its nonce COMMITTED before the cluster is touched, the target checked again,
  a server-side dry run, the write, a read-back, an audit row: inside the 10 s tool limit.
* health_check: rollout-complete status, readiness, and the oracle's own HTTP probe in two
  rounds, for exactly the allowlisted services, bounded in time.

The constructor, describe() and list_scenarios() never touch the cluster or create a file:
the runtime builds an adapter for every GET /system and every run start.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from recap.core.errors import CapabilityError
from recap.core.ids import utcnow
from recap.core.interfaces import TargetEnvironment
from recap.core.models import EnvironmentInfo, ScenarioInfo
from recap.core.observations import (
    ActionOutcome,
    AlertSignal,
    AuthorizedAction,
    ClusterEvent,
    DeploymentList,
    DeploymentRecord,
    EventList,
    GroundTruthHealth,
    HealthReport,
    HealthSnapshot,
    LogLine,
    LogQueryResult,
    MetricName,
    MetricPoint,
    MetricSeries,
    RollbackPlan,
    ServiceList,
)
from recap.core.signals import ERROR_RATE_THRESHOLD
from recap.environments.kubernetes import probe, rollout, translate
from recap.environments.kubernetes.fake import STATE_FILE as FAKE_STATE_FILE
from recap.environments.kubernetes.fake import FakeCluster, FakeKubeClient
from recap.environments.kubernetes.kubectl import (
    VERSION_TAG,
    CommandRecord,
    KubeClient,
    KubectlClient,
    KubeError,
    grace,
    sanitize,
)
from recap.environments.kubernetes.metrics import UNITS, MetricsUnavailable, PrometheusClient
from recap.environments.kubernetes.options import (
    DNS_LABEL,
    IDENTITIES,
    Identity,
    KindOptions,
    parse_options,
)
from recap.environments.kubernetes.preflight import (
    PreflightError,
    check_kubeconfig,
    read_fingerprint,
)
from recap.environments.kubernetes.probe import AlertRule, Condition, ProbeResult
from recap.environments.kubernetes.scenario import (
    HttpBurst,
    KindScenarioSpec,
    SetImage,
    list_kind_scenarios,
    load_kind_scenario,
    to_info,
)
from recap.environments.kubernetes.state import (
    NOT_RESET,
    Baseline,
    NamespaceLease,
    RunInfo,
    RunState,
    writes_disabled,
)
from recap.safety.capabilities import CapabilityPayload, verify_capability

SUPPORTED_ACTIONS = ("rollback", "restart")
METRICS = ("error_rate", "latency_p95_ms", "memory_mb", "cpu_pct", "restarts")
# The run id becomes a file name, so it may not contain path separators.
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
MAX_LOG_PODS = 6  # two replicas, a surge pod, and a few left over from a stuck rollout
LOG_BYTES_PER_CONTAINER = 256_000
WRITE_REQUEST_TIMEOUT = 2.0  # each kubectl call on the write path: well inside 10 s in total
SOURCE = "recap-kind-probe"


class KubernetesEnvironment(TargetEnvironment):
    """RECAP's adapter for its local Kind demo cluster (or, for tests, an offline fake)."""

    kind = "kind"

    def __init__(self, run_id: str, **options: Any) -> None:
        if not isinstance(run_id, str) or not _SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError(f"invalid run_id {str(run_id)[:64]!r}")
        self.run_id = run_id
        self.options: KindOptions = parse_options(options)
        self.namespace = self.options.namespace
        self.services = tuple(self.options.services)
        state_dir = Path(self.options.state_dir)
        self.state = RunState(state_dir / f"kind-{run_id}.db")
        self.lease = NamespaceLease(state_dir / "kind-leases.db")
        # Built on first use, never in the constructor (no I/O here).
        self._clients: dict[Identity, KubeClient] = {}
        self._pinned: set[Identity] = set()
        self._fake: FakeCluster | None = None

    # --- lifecycle (trusted) ------------------------------------------------------------

    def describe(self) -> EnvironmentInfo:
        """Static and offline: what this target is. No path, URL or secret."""
        options = self.options
        fake = options.client == "fake"
        what = (
            f"Local Kind cluster {options.cluster} (context {options.context}), not production: "
            f"{len(self.services)} demo services in namespace {self.namespace}, read through "
            "typed kubectl operations. The only writes are a rollback to a named revision and "
            "a rolling restart, each with a signed single-use capability, inside the cluster's "
            "RBAC and admission policy."
        )
        if fake:
            what = (
                "OFFLINE FAKE of the local Kind demo cluster, for tests: synthetic Kubernetes "
                "data from a seed folder, no cluster, no kubectl, no network. " + what
            )
        return EnvironmentInfo(
            kind=self.kind,
            name=f"Kind cluster {options.cluster}" + (" (offline fake)" if fake else ""),
            namespace=self.namespace,
            is_simulated=False,
            supported_actions=list(SUPPORTED_ACTIONS),
            description=what,
            stabilization_timeout_seconds=options.stabilization_timeout_seconds,
            stabilization_poll_seconds=options.stabilization_poll_seconds,
        )

    def list_scenarios(self) -> list[ScenarioInfo]:
        """The Kind scenario files (fixtures/kind-scenarios/), sorted by id."""
        return [to_info(spec) for spec in list_kind_scenarios(self.options.scenarios_dir)]

    async def reset(self, scenario_id: str, seed: int) -> AlertSignal:
        """Baseline, calm, fault, alert: every step bounded, nothing about the fault leaked."""
        spec = load_kind_scenario(self.options.scenarios_dir, scenario_id)
        self._check_scenario(spec)
        self.state.start(
            run_id=self.run_id,
            scenario_id=spec.id,
            seed=seed,
            namespace=self.namespace,
            started_at=utcnow().isoformat(),
        )
        await self.preflight()
        self.lease.take(self.namespace, self.run_id, self.options.lease_seconds)
        try:
            baselines = await self._restore_baseline(spec)
            await self._wait_calm()
            baseline_at = utcnow()
            margin = timedelta(seconds=self.options.baseline_margin_seconds)
            self.state.put(
                baselines={
                    s: {"version": b.version, "revision": b.revision} for s, b in baselines.items()
                },
                baseline_at=baseline_at.isoformat(),
                observation_start=(baseline_at - margin).isoformat(),
            )
            for step in spec.faults:
                await self._apply(step)
            alert = await self._await_alert(spec.alert)
        except BaseException:
            self.lease.release(self.namespace, self.run_id)  # no run starts: free the namespace
            raise
        self.state.put(reset_complete=True, fired_at=alert.fired_at.isoformat())
        return alert

    async def preflight(self) -> None:
        """Prove this is the pinned cluster, and that its clocks and versions line up."""
        self._pinned.clear()  # every reset checks the kubeconfigs again
        for identity in IDENTITIES:
            self._pin(identity)
        versions = await self._client("reader").version()
        _check_version_skew(versions)
        server = await self._prometheus().server_time()
        skew = abs((server - utcnow()).total_seconds())
        if skew > self.options.max_clock_skew_seconds:
            raise PreflightError(
                f"the cluster's clock is {skew:.1f} s away from this machine's (more than "
                f"{self.options.max_clock_skew_seconds:g} s): observations and the oracle would "
                "disagree on time; restart the container VM to resync its clock"
            )

    def release_lease(self) -> bool:
        """Give this run's namespace lease up (trusted code or an operator; not an MCP tool)."""
        return self.lease.release(self.namespace, self.run_id)

    # --- observe (read-only; exposed to agents as MCP tools) ----------------------------

    async def observe_services(self) -> ServiceList:
        """The allowlisted Deployments: version (image tag), replicas, dependencies."""
        self._run()
        deployments = {
            translate.name_of(d): d for d in await self._client("reader").list_deployments()
        }
        services = [
            translate.service_info(
                deployments[s], self.namespace, self.services, self.options.image_repository
            )
            for s in self.services
            if s in deployments
        ]
        return ServiceList(namespace=self.namespace, services=services)

    async def observe_health(self, service: str) -> HealthSnapshot:
        """Status and readiness from the API; error rate and p95 latency from Prometheus."""
        self._run()
        service = self._service(service)
        reader = self._client("reader")
        deployment = await _get_or_none(reader, service)
        pods = await reader.list_pods(translate.selector_of(deployment)) if deployment else []
        stuck = rollout.progress_deadline_exceeded(deployment) if deployment else False
        status = translate.agent_status(deployment, pods, stuck)
        ready = translate.ready_replicas(deployment) if deployment else 0
        replicas = translate.desired_replicas(deployment) if deployment else 0
        now = utcnow()
        prometheus = self._prometheus()
        try:
            error_rate = await prometheus.instant("error_rate", service, now)
            latency = await prometheus.instant("latency_p95_ms", service, now)
        except MetricsUnavailable as exc:
            raise MetricsUnavailable(
                f"{service} is {status} with {ready} of {replicas} replicas ready, but its "
                f"error rate and latency cannot be measured: {exc}"
            ) from None
        return HealthSnapshot(
            service=service,
            status=status,
            ready_replicas=ready,
            replicas=replicas,
            error_rate=error_rate,
            latency_p95_ms=latency,
            observed_at=now,
        )

    async def observe_logs(self, service: str, limit: int, since_minutes: int) -> LogQueryResult:
        """The newest `limit` lines of the service's pods since this run's window began."""
        info = self._run()
        service = self._service(service)
        limit = _clamp(limit, 1, 100, "limit")
        since_minutes = _clamp(since_minutes, 1, 240, "since_minutes")
        reader = self._client("reader")
        deployment = await _get_or_none(reader, service)
        if deployment is None:
            return LogQueryResult(service=service, lines=[], total_matched=0)
        container = translate.container_of(
            translate.template_of(deployment), service, self.options.image_repository
        )
        if container is None:
            raise KubeError(f"{service} has no container of its own to read logs from")
        every_pod = await reader.list_pods(translate.selector_of(deployment))
        pods = every_pod[:MAX_LOG_PODS]
        start = max(utcnow() - timedelta(minutes=since_minutes), info.observation_start)
        reads = []
        for pod in pods:
            restarted = translate.restart_count([pod]) > 0
            for previous in (False, True) if restarted else (False,):
                reads.append(
                    self._pod_lines(reader, service, pod, container, start, limit, previous)
                )
        results = await asyncio.gather(*reads)
        lines = sorted(
            (line for found, _ in results for line in found if line.timestamp >= start),
            key=lambda line: line.timestamp,
        )
        kept = lines[max(0, len(lines) - limit) :]
        # Honest about what was not read: kubectl's --tail per container, or pods left out.
        cut = any(full for _, full in results) or len(every_pod) > len(pods)
        return LogQueryResult(
            service=service,
            lines=kept,
            total_matched=len(lines),
            truncated=len(kept) < len(lines) or cut,
        )

    async def observe_metrics(
        self, service: str, metric: MetricName, window_minutes: int
    ) -> MetricSeries:
        """One metric of one service, a point per step, since this run's window began."""
        info = self._run()
        service = self._service(service)
        if metric not in METRICS:
            raise ValueError(f"unknown metric '{str(metric)[:40]}'")
        window = _clamp(window_minutes, 5, 240, "window_minutes")
        reader = self._client("reader")
        deployment = await _get_or_none(reader, service)
        if deployment is None:
            raise MetricsUnavailable(f"{service} has no Deployment: no {metric} to report")
        now = utcnow()
        if metric == "restarts":
            pods = await reader.list_pods(translate.selector_of(deployment))
            points = [MetricPoint(timestamp=now, value=float(translate.restart_count(pods)))]
            return MetricSeries(
                service=service, metric=metric, unit=UNITS[metric], points=points, step_minutes=1
            )
        step_minutes = max(1, math.ceil(window / self.options.metrics_max_points))
        step = timedelta(minutes=step_minutes)
        begin = max(now - timedelta(minutes=window), info.observation_start)
        start = now - step * int((now - begin) / step)
        points = await self._prometheus().range(
            metric, service, start, now, int(step.total_seconds())
        )
        limit = None
        repository = self.options.image_repository
        if metric == "memory_mb":
            memory = translate.limit_of(deployment, service, repository, "memory")
            limit = memory / 1048576 if memory else None
        elif metric == "cpu_pct":
            cores = translate.limit_of(deployment, service, repository, "cpu")
            if not cores:
                raise MetricsUnavailable(f"{service} has no CPU limit to measure cpu_pct against")
            points = [
                MetricPoint(timestamp=p.timestamp, value=100.0 * p.value / cores) for p in points
            ]
        return MetricSeries(
            service=service,
            metric=metric,
            unit=UNITS[metric],
            points=points,
            step_minutes=step_minutes,
            limit=limit,
        )

    async def observe_deployments(self, service: str | None, limit: int) -> DeploymentList:
        """Releases since this run's baseline (restarts folded in), newest first."""
        info = self._run()
        services = [self._service(service)] if service is not None else list(self.services)
        limit = _clamp(limit, 0, 50, "limit")
        reader = self._client("reader")
        deployments = {translate.name_of(d): d for d in await reader.list_deployments()}

        async def releases_of(name: str) -> list[DeploymentRecord]:
            deployment = deployments.get(name)
            if deployment is None:
                return []
            selector = translate.selector_of(deployment)
            replicasets, pods = await asyncio.gather(
                reader.list_replicasets(selector), reader.list_pods(selector)
            )
            found = rollout.revisions(deployment, replicasets, name, self.options.image_repository)
            return rollout.releases(
                name,
                found,
                baseline=info.baselines.get(name),
                baseline_at=info.baseline_at,
                log=self.state.release_log(name),
                pod_times=_first_pod_times(pods),
            )

        # One round of calls for every service at once: the tool call has 10 s in all.
        per_service = await asyncio.gather(*(releases_of(name) for name in services))
        records = [record for found in per_service for record in found]
        records.sort(key=lambda record: record.deployed_at, reverse=True)
        return DeploymentList(deployments=records[:limit])

    async def observe_events(self, service: str | None, limit: int) -> EventList:
        """Events of the allowlisted services since this run's window began, newest first;
        OOMKilled terminations from pod status are added, labelled as such."""
        info = self._run()
        wanted = self._service(service) if service is not None else None
        limit = _clamp(limit, 0, 100, "limit")
        reader = self._client("reader")
        deployments, pods, raw = await asyncio.gather(
            reader.list_deployments(), reader.list_pods(None), reader.list_events()
        )
        owners = self._owners(deployments, pods)
        terminations = translate.last_terminations(pods)
        events: list[ClusterEvent] = []
        for event in raw:
            involved = event.get("involvedObject") or event.get("regarding") or {}
            kind, name = str(involved.get("kind")), str(involved.get("name"))
            owner = owners.service_of(kind, name)
            ended = terminations.get(name, set()) if kind == "Pod" else set()
            translated = translate.cluster_event(event, owner, ended) if owner else None
            if translated is not None:
                events.append(translated)
        for name in self.services:
            mine = [p for p in pods if owners.service_of("Pod", translate.name_of(p)) == name]
            events += translate.oom_kills(mine, name)
        events = [
            e
            for e in events
            if e.timestamp >= info.observation_start and wanted in (None, e.service)
        ]
        events.sort(key=lambda event: event.timestamp, reverse=True)
        return EventList(events=events[:limit])

    async def plan_rollback(self, service: str) -> RollbackPlan:
        """Dry run from the rollout history: which version a rollback would restore."""
        info = self._run()
        service = self._service(service)
        reader = self._client("reader")
        deployment = await _get_or_none(reader, service)
        if deployment is None:
            return RollbackPlan(
                service=service,
                current_version=translate.UNKNOWN_VERSION,
                possible=False,
                reason=f"{service} has no Deployment",
            )
        replicasets = await reader.list_replicasets(translate.selector_of(deployment))
        found = rollout.revisions(deployment, replicasets, service, self.options.image_repository)
        return rollout.rollback_plan(service, found, info.baselines.get(service))

    # --- act (the single write path; the final privilege barrier) ----------------------

    def bind_executor(self, public_key: str) -> None:
        """Trust capabilities signed by this run's key: stored in the run's local file,
        where the MCP server's adapter reads it; never in the cluster."""
        self.state.bind_key(public_key)

    async def execute_authorized(self, action: AuthorizedAction) -> ActionOutcome:
        """Rollback or restart, only with a valid capability for exactly this action."""
        deadline = time.monotonic() + self.options.write_deadline_seconds
        # 1. Only the two reversible actions exist here; delete_namespace never does, even
        #    with a valid capability (the gateway's R9 is the first lock, this the second).
        if action.action not in SUPPORTED_ACTIONS:
            return self._refused(
                action, f"not supported: this target performs only {', '.join(SUPPORTED_ACTIONS)}"
            )
        # 2. The write switch: while it is on, nothing is written (reads keep working).
        if writes_disabled(self.options.write_switch_path()):
            return self._refused(action, "refused: writes are switched off for this target")
        # 3. The capability, verified against the bound key and the used nonces; its nonce is
        #    COMMITTED before the cluster is touched (a crash burns it; it never works twice).
        try:
            self.state.consume_capability(lambda key, used: self._verify(action, key, used))
        except CapabilityError as exc:
            return self._refused(action, f"not authorized: {exc}")
        # 4. The target, checked again by the adapter itself.
        try:
            service = self._service(action.target)
        except ValueError as exc:
            return self._refused(action, f"refused: {exc}")
        holder = self.lease.holder(self.namespace)
        if holder is not None and holder.run_id != self.run_id:
            return self._refused(
                action, f"refused: namespace {self.namespace} belongs to another run now"
            )
        version = action.params.get("to_version") if action.action == "rollback" else None
        if action.action == "rollback" and not (
            isinstance(version, str) and VERSION_TAG.fullmatch(version)
        ):
            return self._refused(action, "refused: a rollback needs a target version vX.Y.Z")
        # 5-7. Dry run, write, read-back, audit. Shielded: a cancelled tool call must never
        #      cut a write in half; the write finishes, is audited, and only then returns.
        write = asyncio.ensure_future(self._write(action, service, version, deadline))
        return await asyncio.shield(write)

    # --- oracle (trusted; never exposed as an MCP tool) ---------------------------------

    async def health_check(self) -> HealthReport:
        """Ground truth for exactly the allowlisted services, from two rounds a few seconds
        apart: rollout complete, readiness, and the oracle's own HTTP probe."""
        first = await self._ground_truth()
        await asyncio.sleep(self.options.probe_round_gap_seconds)
        second = await self._ground_truth()
        return HealthReport(checked_at=utcnow(), services=probe.combine_rounds(first, second))

    # ==================================================================================
    # Reset steps
    # ==================================================================================

    def _check_scenario(self, spec: KindScenarioSpec) -> None:
        if set(spec.baseline) != set(self.services):
            raise ValueError(
                f"scenario '{spec.id}' must give a baseline for exactly {list(self.services)}"
            )

    async def _restore_baseline(self, spec: KindScenarioSpec) -> dict[str, Baseline]:
        """Put every service back on its baseline image (as recap-scenario) and wait until
        it is healthy. A fault-free baseline is the precondition of a valid run."""
        scenario = self._client("scenario")
        for service, version in spec.baseline.items():
            deployment = await scenario.get_deployment(service)
            image = translate.image_of(deployment, service, self.options.image_repository)
            container = self._container(deployment, service)
            if translate.version_of(image) != version:
                await scenario.set_image(service, container, version, dry_run=True)
                await scenario.set_image(service, container, version, dry_run=False)
            elif _settled_but_unready(deployment):
                # The right image, but no rollout under way and pods not ready: e.g. a memory
                # left full by an earlier run. Fresh pods are the baseline.
                await scenario.rollout_restart(service, dry_run=True)
                await scenario.rollout_restart(service, dry_run=False)
        await self._wait_healthy(self.options.baseline_timeout_seconds)
        baselines: dict[str, Baseline] = {}
        for service, version in spec.baseline.items():
            deployment = await scenario.get_deployment(service)
            replicasets = await scenario.list_replicasets(translate.selector_of(deployment))
            found = rollout.revisions(
                deployment, replicasets, service, self.options.image_repository
            )
            live = rollout.current(found)
            if live is None or live.version != version:
                raise RuntimeError(f"{service} did not settle on its baseline {version}")
            baselines[service] = Baseline(version=version, revision=live.number)
        return baselines

    async def _wait_healthy(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            problems = await self._baseline_problems()
            if not problems:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the baseline did not become healthy within {seconds:g} s: "
                    + "; ".join(problems)
                )
            await asyncio.sleep(self.options.alert_poll_seconds)

    async def _baseline_problems(self) -> list[str]:
        view = await self._view("scenario")
        probes = await self._probe_round()
        problems = []
        for service in self.services:
            deployment, pods = view[service]
            if deployment is None:
                problems.append(f"{service} has no Deployment")
                continue
            complete, why = rollout.rollout_state(deployment)
            if not complete:
                problems.append(f"{service}: {why}")
            elif translate.crash_looping(pods):
                problems.append(f"{service}: a container is crash-looping")
            if probes[service].error_rate >= ERROR_RATE_THRESHOLD:
                problems.append(f"{service}: probe error rate {probes[service].error_rate:.2f}")
        return problems

    async def _wait_calm(self) -> None:
        """Wait until Prometheus shows no error burst over the baseline margin: an earlier
        run's errors inside the window would make this run's onset unreadable (V2)."""
        margin = self.options.baseline_margin_seconds
        deadline = time.monotonic() + self.options.calm_timeout_seconds
        prometheus = self._prometheus()
        while True:
            end = utcnow()
            start = end - timedelta(seconds=margin)
            noisy: list[str] = []
            for service in self.services:
                try:
                    points = await prometheus.range("error_rate", service, start, end, 30)
                except MetricsUnavailable as exc:
                    noisy.append(str(exc))
                    continue
                worst = max(point.value for point in points)
                if worst > ERROR_RATE_THRESHOLD:
                    noisy.append(
                        f"{service}'s error rate reached {worst:.2f} in the last {margin:g} s"
                    )
            if not noisy:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("the baseline never became calm: " + "; ".join(noisy))
            await asyncio.sleep(self.options.alert_poll_seconds)

    async def _apply(self, step: SetImage | HttpBurst) -> None:
        """One typed fault step, as recap-scenario (or plain HTTP for a burst)."""
        if isinstance(step, SetImage):
            scenario = self._client("scenario")
            deployment = await scenario.get_deployment(step.service)
            container = self._container(deployment, step.service)
            previous = translate.version_of(
                translate.image_of(deployment, step.service, self.options.image_repository)
            )
            await scenario.set_image(step.service, container, step.version, dry_run=True)
            await scenario.set_image(step.service, container, step.version, dry_run=False)
            self.state.log_release(step.service, step.version, previous, "release")
            return
        url = self.options.service_url(step.service, step.path)
        await probe.burst(
            url,
            step.requests,
            step.concurrency,
            self.options.probe_timeout_seconds * 5,
            self._http_transport(),
        )

    async def _await_alert(self, rule: AlertRule) -> AlertSignal:
        """Probe until the rule fires `rule.rounds` times in a row (bounded), then build the
        alert from what was measured: no fault name, no label, no expected answer."""
        deadline = time.monotonic() + rule.timeout_seconds
        streak = 0
        while True:
            view = await self._view("scenario")
            probes = await self._probe_round()
            conditions = {
                service: _condition(deployment, pods)
                for service, (deployment, pods) in view.items()
            }
            fired = probe.alert_symptoms(rule, probes, conditions)
            streak = streak + 1 if fired else 0
            if streak >= rule.rounds:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the alert rule did not fire within {rule.timeout_seconds:g} s after the "
                    "fault steps were applied; the fault did not take visible effect"
                )
            await asyncio.sleep(self.options.alert_poll_seconds)
        affected = [s for s in self.services if s in fired]
        primary = rule.service if rule.service in fired else affected[0]
        ordered = [primary, *(s for s in affected if s != primary)]
        return AlertSignal(
            service=primary,
            affected_services=ordered,
            symptoms=[symptom for s in ordered for symptom in fired[s]],
            namespace=self.namespace,
            fired_at=utcnow(),
            metadata={"source": SOURCE, "namespace": self.namespace},
        )

    # ==================================================================================
    # The write
    # ==================================================================================

    def _verify(
        self, action: AuthorizedAction, key: str | None, used: set[str]
    ) -> CapabilityPayload:
        return verify_capability(
            action.capability,
            public_key=key,
            run_id=self.run_id,
            action=action,
            used_nonces=used,
            allow_safety_off=False,  # a real target never accepts a safety-off capability
        )

    async def _write(
        self, action: AuthorizedAction, service: str, version: str | None, deadline: float
    ) -> ActionOutcome:
        """Steps 5 to 7: resolve, dry run, write, read back, audit. Never past `deadline`.

        Refused before the write was sent: an ActionOutcome with ok=False (nothing changed).
        Sent but failed, or not confirmed by the read-back: audited, then raised, so the
        caller sees a tool error and RECAP lets its oracle judge what really happened (the
        controller waits for the target to settle) instead of taking it for a refusal.
        """
        writer = self._client("writer", request_timeout=WRITE_REQUEST_TIMEOUT)
        call = WRITE_REQUEST_TIMEOUT + grace(WRITE_REQUEST_TIMEOUT)  # one call, at worst
        detail: dict[str, Any] = {"service": service, "write_sent": False}
        started = time.monotonic()
        target: rollout.Revision | None = None
        try:
            before = await writer.get_deployment(service)
            repository = self.options.image_repository
            detail["generation_before"] = _generation(before)
            detail["image_before"] = translate.image_of(before, service, repository)
            detail["revision_before"] = translate.annotations_of(before).get(rollout.REVISION)
            if action.action == "rollback":
                replicasets = await writer.list_replicasets(translate.selector_of(before))
                found = rollout.revisions(before, replicasets, service, repository)
                target = rollout.revision_for(found, str(version))
                detail["to_revision"] = target.number
            if deadline - time.monotonic() < 2 * call:
                raise rollout.RolloutError("not enough time left in this tool call to write safely")
            if target is not None:
                await writer.rollout_undo(service, target.number, dry_run=True)
                detail["dry_run"] = "passed"
                detail["write_sent"] = True
                await writer.rollout_undo(service, target.number, dry_run=False)
            else:
                await writer.rollout_restart(service, dry_run=True)
                detail["dry_run"] = "passed"
                detail["write_sent"] = True
                await writer.rollout_restart(service, dry_run=False)
        except Exception as exc:
            detail["seconds"] = round(time.monotonic() - started, 3)
            reason = sanitize(str(exc) or type(exc).__name__)
            if not detail["write_sent"]:
                return self._refused(action, f"not executed: {reason}", detail=detail)
            self._audit_write(action, "failed", {**detail, "reason": reason})
            raise KubeError(f"the write may have reached the cluster: {reason}") from None
        read_back = await self._read_back(writer, action, service, version, before, deadline, call)
        detail.update(read_back)
        detail["seconds"] = round(time.monotonic() - started, 3)
        if not read_back.get("confirmed"):
            self._audit_write(action, "unconfirmed", detail)
            raise KubeError(
                f"{service}: the write was sent, but the read-back did not confirm it "
                f"({read_back.get('why', 'unknown')}); the oracle will judge"
            )
        previous = translate.version_of(detail["image_before"])
        if action.action == "rollback":
            self.state.log_release(service, str(version), previous, "rollback")
        else:
            self.state.log_release(
                service, previous or translate.UNKNOWN_VERSION, previous, "restart"
            )
        self._audit_write(action, "executed", detail)
        if target is not None:
            message = f"{service}: rollout to revision {target.number} ({version}) started"
        else:
            message = f"{service}: rolling restart started"
        generations = f"{detail['generation_before']} -> {read_back.get('generation_after')}"
        effects = [f"{service} generation {generations}"]
        if read_back.get("image_after"):
            effects.append(f"{service} pod template image: {read_back['image_after']}")
        return ActionOutcome(
            ok=True,
            action=action.action,
            target=service,
            message=message,
            side_effects=effects,
            executed_at=utcnow(),
        )

    async def _read_back(
        self,
        writer: KubeClient,
        action: AuthorizedAction,
        service: str,
        version: str | None,
        before: Mapping[str, Any],
        deadline: float,
        call: float,
    ) -> dict[str, Any]:
        """Step 7: read the Deployment again and check the change is the approved one."""
        if deadline - time.monotonic() < call:
            return {"confirmed": False, "why": "no time left to read the Deployment back"}
        try:
            after = await writer.get_deployment(service)
        except KubeError as exc:
            return {"confirmed": False, "why": f"read-back failed: {exc}"}
        repository = self.options.image_repository
        image_after = translate.image_of(after, service, repository)
        result: dict[str, Any] = {
            "generation_after": _generation(after),
            "image_after": image_after,
            "revision_after": translate.annotations_of(after).get(rollout.REVISION),
        }
        changed = (_generation(after) or 0) > (_generation(before) or 0)
        if action.action == "rollback":
            confirmed = changed and image_after == f"{repository}/{service}:{version}"
        else:
            confirmed = changed and _restarted_at(after) != _restarted_at(before)
        result["confirmed"] = confirmed
        if not confirmed:
            result["why"] = "the Deployment does not show the approved change"
        return result

    def _refused(
        self,
        action: AuthorizedAction,
        reason: str,
        *,
        outcome: str = "refused",
        detail: dict[str, Any] | None = None,
    ) -> ActionOutcome:
        """Refuse (or report a failed write) and audit it, without the capability."""
        self._audit_write(action, outcome, {**(detail or {}), "reason": reason})
        return ActionOutcome(
            ok=False,
            action=action.action,
            target=action.target,
            message=reason,
            executed_at=utcnow(),
        )

    def _audit_write(self, action: AuthorizedAction, outcome: str, detail: dict[str, Any]) -> None:
        if not self.state.exists():
            return  # no run to audit into (never reset): nothing was written either
        self.state.record_write(
            action=action.action,
            target=action.target[:64],
            approval_id=action.approval_id[:128],
            proposal_digest=action.proposal_digest[:128],
            outcome=outcome,
            detail=detail,
        )

    # ==================================================================================
    # Ground truth (the oracle) and shared views
    # ==================================================================================

    async def _ground_truth(self) -> dict[str, GroundTruthHealth]:
        view = await self._view("oracle")
        probes = await self._probe_round()
        found: dict[str, GroundTruthHealth] = {}
        for service in self.services:
            deployment, pods = view[service]
            complete = rollout.rollout_state(deployment)[0] if deployment else False
            found[service] = GroundTruthHealth(
                service=service,
                status=translate.ground_status(deployment, pods, complete),
                ready_replicas=translate.ready_replicas(deployment) if deployment else 0,
                replicas=translate.desired_replicas(deployment) if deployment else 0,
                error_rate=probes[service].error_rate,
            )
        return found

    async def _view(
        self, identity: Identity
    ) -> dict[str, tuple[dict[str, Any] | None, list[dict[str, Any]]]]:
        """Each allowlisted service's Deployment (None if gone) and its pods."""
        client = self._client(identity)
        deployments: dict[str, dict[str, Any] | None]
        pods: list[dict[str, Any]]
        if identity == "scenario":  # recap-scenario may only GET the two named Deployments
            gets = asyncio.gather(*(_get_or_none(client, s) for s in self.services))
            found, pods = await asyncio.gather(gets, client.list_pods(None))
            deployments = dict(zip(self.services, found, strict=True))
        else:
            listed: list[dict[str, Any]]
            listed, pods = await asyncio.gather(client.list_deployments(), client.list_pods(None))
            by_name = {translate.name_of(d): d for d in listed}
            deployments = {s: by_name.get(s) for s in self.services}
        view: dict[str, tuple[dict[str, Any] | None, list[dict[str, Any]]]] = {}
        for service, deployment in deployments.items():
            if deployment is None:
                view[service] = (None, [])
                continue
            selector = translate.selector_of(deployment)
            mine = [p for p in pods if translate.matches(translate.labels_of(p), selector)]
            view[service] = (deployment, mine)
        return view

    def _owners(self, deployments: list[dict[str, Any]], pods: list[dict[str, Any]]) -> _Owners:
        """Which allowlisted service each Deployment, ReplicaSet and pod belongs to."""
        exact: dict[tuple[str, str], str] = {}
        for deployment in deployments:
            service = translate.name_of(deployment)
            if service not in self.services:
                continue
            exact[("Deployment", service)] = service
            selector = translate.selector_of(deployment)
            for pod in pods:
                if not translate.matches(translate.labels_of(pod), selector):
                    continue
                exact[("Pod", translate.name_of(pod))] = service
                for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
                    if isinstance(owner, dict) and owner.get("kind") == "ReplicaSet":
                        exact[("ReplicaSet", str(owner.get("name")))] = service
        return _Owners(exact, self.services)

    async def _probe_round(self) -> dict[str, ProbeResult]:
        urls = {service: self.options.probe_url(service) for service in self.services}
        return await probe.probe_round(
            urls,
            self.options.probe_requests,
            self.options.probe_timeout_seconds,
            self._http_transport(),
        )

    async def _pod_lines(
        self,
        reader: KubeClient,
        service: str,
        pod: Mapping[str, Any],
        container: Mapping[str, Any],
        start: datetime,
        limit: int,
        previous: bool,
    ) -> tuple[list[LogLine], bool]:
        """One container's lines (and whether kubectl's --tail may have cut some)."""
        try:
            text = await reader.pod_logs(
                translate.name_of(pod),
                str(container.get("name")),
                since_time=start,
                tail=limit,
                limit_bytes=LOG_BYTES_PER_CONTAINER,
                previous=previous,
            )
        except KubeError:
            return [], False  # e.g. a container still waiting to start: no lines yet
        found = translate.log_lines(service, text)
        full = len(found) >= limit or len(text.encode()) >= LOG_BYTES_PER_CONTAINER
        return found, full

    def _container(self, deployment: Mapping[str, Any], service: str) -> str:
        container = translate.container_of(
            translate.template_of(deployment), service, self.options.image_repository
        )
        if container is None or not isinstance(container.get("name"), str):
            raise KubeError(f"{service} has no container of its own")
        return str(container["name"])

    # ==================================================================================
    # Plumbing: identities, clients, checks
    # ==================================================================================

    def _run(self) -> RunInfo:
        """This run's baseline; RuntimeError('environment not reset') before a complete reset."""
        return self.state.info()

    def _service(self, service: object) -> str:
        """The adapter's own allowlist, checked again: a Kubernetes name it may touch."""
        if (
            not isinstance(service, str)
            or service not in self.services
            or not DNS_LABEL.fullmatch(service)
        ):
            raise ValueError(f"unknown service '{str(service)[:64]}'")
        return service

    def _pin(self, identity: Identity) -> None:
        """Fail closed unless this identity's kubeconfig reaches the pinned cluster."""
        if identity in self._pinned:
            return
        options = self.options
        path = options.kubeconfig_path(identity)
        if options.client == "kubectl" or path is not None:
            fingerprint = read_fingerprint(options.fingerprint_path())
            check_kubeconfig(identity, path, options.context, fingerprint)
        if options.client == "kubectl":
            binary = options.kubectl_path()
            if binary is None or not (binary.is_file() and os.access(binary, os.X_OK)):
                raise PreflightError("kubectl was not found at the configured absolute path")
        self._pinned.add(identity)

    def _client(self, identity: Identity, *, request_timeout: float | None = None) -> KubeClient:
        """One identity's typed client, built (and pinned) on first use."""
        key = identity
        if request_timeout is None and key in self._clients:
            return self._clients[key]
        self._pin(identity)
        options = self.options
        settings: dict[str, Any] = {
            "identity": identity,
            "context": options.context,
            "namespace": self.namespace,
            "request_timeout": request_timeout or options.request_timeout_seconds,
            "repository": options.image_repository,
            "services": self.services,
            "audit": self._record_command,
        }
        client: KubeClient
        if options.client == "fake":
            client = FakeKubeClient(self.fake_cluster(), **settings)
        else:
            kubeconfig = options.kubeconfig_path(identity)
            assert kubeconfig is not None  # _pin refused a missing one
            binary = options.kubectl_path()
            assert binary is not None  # parse_options requires it for client "kubectl"
            client = KubectlClient(
                kubectl=binary,
                kubeconfig=kubeconfig,
                home=Path(options.state_dir) / "kubectl-home",
                **settings,
            )
        if request_timeout is None:
            self._clients[key] = client
        return client

    def fake_cluster(self) -> FakeCluster:
        """The offline fake (client: fake only); built on first use."""
        if self.options.client != "fake":
            raise RuntimeError("this adapter talks to a real cluster, not to the fake")
        if self._fake is None:
            self._fake = FakeCluster(
                self.options.fake_data_path(),
                Path(self.options.state_dir) / FAKE_STATE_FILE,
                namespace=self.namespace,
                prometheus_url=self.options.prometheus_url,
                service_urls=self.options.service_urls,
            )
        return self._fake

    def _http_transport(self) -> httpx.AsyncBaseTransport | None:
        return self.fake_cluster().http_transport() if self.options.client == "fake" else None

    def _prometheus(self) -> PrometheusClient:
        return PrometheusClient(
            self.options.prometheus_url,
            self.namespace,
            self.options.metrics_rate_window,
            transport=self._http_transport(),
        )

    def _record_command(self, record: CommandRecord) -> None:
        """The run's command audit (trusted local data: never shown to a model)."""
        if self.state.exists():
            self.state.record_command(record)


# --- helpers ----------------------------------------------------------------------------------


class _Owners:
    """Which allowlisted service an event's object belongs to: known objects by name, and
    objects that are gone (an old ReplicaSet, a replaced pod) by the names Deployments give
    them (<deployment>-<hash>, <deployment>-<hash>-<suffix>)."""

    def __init__(self, exact: Mapping[tuple[str, str], str], services: tuple[str, ...]) -> None:
        self.exact = dict(exact)
        self.patterns = {
            service: (
                re.compile(rf"^{re.escape(service)}-[a-z0-9]{{1,10}}$"),
                re.compile(rf"^{re.escape(service)}-[a-z0-9]{{1,10}}-[a-z0-9]{{5}}$"),
            )
            for service in services
        }

    def service_of(self, kind: str, name: str) -> str | None:
        found = self.exact.get((kind, name))
        if found is not None or kind not in ("ReplicaSet", "Pod"):
            return found
        for service, (replicaset, pod) in self.patterns.items():
            if (replicaset if kind == "ReplicaSet" else pod).fullmatch(name):
                return service
        return None


async def _get_or_none(client: KubeClient, name: str) -> dict[str, Any] | None:
    """The Deployment, or None when the API says it does not exist."""
    try:
        return await client.get_deployment(name)
    except KubeError as exc:
        if "(NotFound)" in str(exc):
            return None
        raise


def _settled_but_unready(deployment: Mapping[str, Any]) -> bool:
    """No rollout under way (every replica updated, no surge pod), yet replicas not ready."""
    status = deployment.get("status") or {}
    desired = translate.desired_replicas(deployment)
    settled = status.get("updatedReplicas") == desired and status.get("replicas") == desired
    return settled and translate.ready_replicas(deployment) < desired


def _condition(deployment: Mapping[str, Any] | None, pods: list[dict[str, Any]]) -> Condition:
    if deployment is None:
        return Condition(ready=0, replicas=1, crash_looping=False, rollout_stuck=False)
    return Condition(
        ready=translate.ready_replicas(deployment),
        replicas=translate.desired_replicas(deployment),
        crash_looping=translate.crash_looping(pods),
        rollout_stuck=rollout.progress_deadline_exceeded(deployment),
    )


def _first_pod_times(pods: list[dict[str, Any]]) -> dict[str, datetime]:
    """ReplicaSet name -> the creation time of its oldest current pod."""
    times: dict[str, datetime] = {}
    for pod in pods:
        created = translate.created_at(pod)
        for owner in (pod.get("metadata") or {}).get("ownerReferences") or []:
            if isinstance(owner, dict) and owner.get("kind") == "ReplicaSet" and created:
                name = str(owner.get("name"))
                times[name] = min(times.get(name, created), created)
    return times


def _generation(deployment: Mapping[str, Any]) -> int | None:
    value = (deployment.get("metadata") or {}).get("generation")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _restarted_at(deployment: Mapping[str, Any]) -> str | None:
    template = translate.template_of(deployment)
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    value = annotations.get(rollout.RESTARTED_AT) if isinstance(annotations, dict) else None
    return str(value) if value else None


def _clamp(value: object, low: int, high: int, what: str) -> int:
    """Bound a number a direct MCP caller may send unchecked (the gateway checks too)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    return max(low, min(high, value))


def _check_version_skew(versions: Mapping[str, Any]) -> None:
    """kubectl is supported within one minor version of the API server."""

    def minor(key: str) -> int | None:
        entry = versions.get(key)
        raw = str(entry.get("minor", "")) if isinstance(entry, dict) else ""
        digits = re.match(r"\d+", raw)
        return int(digits.group()) if digits else None

    client, server = minor("clientVersion"), minor("serverVersion")
    if client is None or server is None:
        raise PreflightError("kubectl or the API server did not report its version")
    if abs(client - server) > 1:
        raise PreflightError(
            f"kubectl 1.{client} and the API server 1.{server} are more than one minor "
            "version apart (unsupported skew)"
        )


__all__ = ["NOT_RESET", "KubernetesEnvironment"]
