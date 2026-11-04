"""An offline fake of the demo cluster, for tests. It is NOT Kubernetes.

`client: fake` makes the Kind adapter run against this fake instead of kubectl, so the
adapter, the real MCP server subprocess, the gateway and the controller can be tested
without a cluster, without Docker and without a network. describe() says so on every run.

What it imitates, and how honestly:

* Kubernetes objects: Deployments, ReplicaSets, Pods and Events, in the JSON shapes
  `kubectl -o json` prints (hand-written from the Kubernetes API reference: synthetic, not
  recorded from a cluster). Writes follow Kubernetes' rollout rules: a new pod template
  makes a ReplicaSet with the next revision number, going back to an earlier template
  REUSES its ReplicaSet and renumbers it (with a revision-history annotation), a rolling
  restart is a new revision with the same image, a server-side dry run changes nothing.
* The demo app: how each image behaves (error share, latency, memory, a crash at start-up,
  how long each start of a crashing image runs, the log lines it writes) comes from the seed
  folder (tests/fixtures/kind/cluster.json),
  not from code. The frontend fails when its dependency fails or cannot be reached.
* Prometheus and the services' NodePorts: an httpx mock transport that answers the
  adapter's fixed PromQL queries (matched exactly, so a changed template fails loudly) and
  the oracle's probe requests, from the same state.

State lives in one JSON document in SQLite next to the run files (<state_dir>/
kind-fake-cluster.db), so the controller and the MCP server subprocess see one cluster, as
they would see one real cluster. Metric values are the behaviour of whatever image served
at each moment (no rate-window averaging). A rollout completes `rollout_seconds` after it
starts (0: at once); an image that crashes never completes it: the rollout is stuck and the
old pods keep serving, as with maxUnavailable 0.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from recap.core.ids import utcnow
from recap.environments.kubernetes.kubectl import Command, KubeClient, KubeOp
from recap.environments.kubernetes.metrics import PROMETHEUS_METRICS, promql
from recap.environments.kubernetes.rollout import (
    CHANGE_CAUSE,
    RESTARTED_AT,
    REVISION,
    REVISION_HISTORY,
)
from recap.environments.kubernetes.translate import DEPENDS_ON, parse_quantity, version_of
from recap.environments.simulated.state_store import SandboxStateStore

State = dict[str, Any]
SEED_FILE = "cluster.json"
STATE_FILE = "kind-fake-cluster.db"
# The demo's traffic generator sends about 5 requests a second; the fake writes one request
# line every 2 s, counted back from "now", so a fault is visible in the logs at once.
LOG_INTERVAL_SECONDS = 2
LOG_HORIZON_MINUTES = 10  # older lines are not generated (kubectl's --tail cuts them anyway)
_WINDOW = re.compile(r"\[([0-9]+[sm])\]")
_NOW_SLACK = timedelta(seconds=1)
CRASH_BACKOFF_SECONDS = 40  # the wait between a crashed container's starts (the kubelet's grows)


class _KubectlFailure(Exception):
    """What kubectl would print on stderr before exiting with code 1."""


class FakeCluster:
    """The fake demo cluster: Kubernetes objects, the app's behaviour, Prometheus, probes."""

    def __init__(
        self,
        data_dir: Path,
        state_path: Path,
        *,
        namespace: str,
        prometheus_url: str | None,
        service_urls: Mapping[str, str],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.data_dir = data_dir
        self.store = SandboxStateStore(state_path)
        self.namespace = namespace
        # None: a target without a monitoring system (no request is ever answered as one).
        self.prometheus = urlsplit(prometheus_url).netloc if prometheus_url else None
        self.hosts = {urlsplit(url).netloc: service for service, url in service_urls.items()}
        self.clock = clock
        # In-process only (tests): the next call of an operation fails with (code, stderr).
        # Keys: "<op>" for any call, "<op>:dry_run" or "<op>:write" for one of the two.
        self.failures: dict[str, tuple[int, str]] = {}
        self._probe_counts: dict[str, int] = {}
        self._report_counts: dict[str, int] = {}
        self._spec: State | None = None

    # --- the seed -----------------------------------------------------------------------

    def spec(self) -> State:
        """The seed folder's cluster.json: services, image behaviour, versions."""
        if self._spec is None:
            text = (self.data_dir / SEED_FILE).read_text(encoding="utf-8")
            self._spec = json.loads(text)
        return self._spec

    def state(self) -> State:
        """The cluster's state, seeded from the data folder on first use."""
        state = self.store.load()
        if state is None:
            state = self._seed()
            self.store.save(state)
        return state

    def _seed(self) -> State:
        now = self.clock()
        state: State = {
            "deployments": {},
            "replicasets": {},
            "events": [],
            "timeline": [],  # which image served each service from when
            "pressure": [],  # when each service's memory was full (the /report burst)
            "rollouts": {},
            "activations": {},
        }
        for service, entry in self.spec()["services"].items():
            born = now - timedelta(minutes=int(entry.get("age_minutes", 4320)))
            deployment = _deployment(self.namespace, service, entry, born)
            state["deployments"][service] = deployment
            # The current image's own age (image_age_minutes): a fault an external harness
            # injected minutes ago, while the app itself was deployed earlier.
            current: State = {"image": entry["image"]}
            if "image_age_minutes" in entry:
                current["age_minutes"] = entry["image_age_minutes"]
            history = [*entry.get("history", []), current]
            for number, item in enumerate(history, start=1):
                age = item.get("age_minutes")
                created = now - timedelta(minutes=int(age)) if age is not None else born
                template = _template(service, item["image"], entry, restarted_at=None)
                cause = f"release {version_of(item['image']) or 'unknown'}"
                rs = _replicaset(self.namespace, deployment, template, number, cause, created)
                state["replicasets"][_name(rs)] = rs
                state["timeline"].append(
                    {"at": _iso(created), "service": service, "image": item["image"]}
                )
            deployment["metadata"]["annotations"][REVISION] = str(len(history))
        return state

    # --- kubectl ------------------------------------------------------------------------

    def handle(self, command: Command, identity: str) -> tuple[int, str, str]:
        """Answer one validated command like kubectl: (exit code, stdout, stderr)."""
        phase = "dry_run" if command.dry_run else "write"
        for key in (f"{command.op.value}:{phase}", command.op.value):
            if key in self.failures:
                code, stderr = self.failures.pop(key)
                return code, "", stderr
        try:
            result = self._dispatch(command)
        except _KubectlFailure as failure:
            return 1, "", str(failure)
        return 0, result if isinstance(result, str) else json.dumps(result), ""

    def _dispatch(self, command: Command) -> State | str:
        op, params = command.op, command.params
        if op == KubeOp.VERSION:
            versions = self.spec().get("kubernetes", {})
            return {
                "clientVersion": _version(versions.get("client", "v1.37.1")),
                "serverVersion": _version(versions.get("server", "v1.37.0")),
            }
        if op in (KubeOp.ROLLOUT_UNDO, KubeOp.ROLLOUT_RESTART, KubeOp.SET_IMAGE):
            return self._write(command)
        state, now = self.state(), self.clock()
        if op == KubeOp.LIST_DEPLOYMENTS:
            return _list(
                [self._deployment_view(state, s, now) for s in sorted(state["deployments"])]
            )
        if op == KubeOp.GET_DEPLOYMENT:
            _require(state, params["name"])
            return self._deployment_view(state, params["name"], now)
        if op == KubeOp.LIST_REPLICASETS:
            wanted = _labels(params["selector"])
            items = [rs for rs in state["replicasets"].values() if _match(rs, wanted)]
            return _list(sorted(items, key=_name))
        if op == KubeOp.LIST_PODS:
            pods = [pod for s in sorted(state["deployments"]) for pod in self._pods(state, s, now)]
            if params.get("selector"):
                wanted = _labels(params["selector"])
                pods = [pod for pod in pods if _match(pod, wanted)]
            return _list(pods)
        if op == KubeOp.LIST_EVENTS:
            return _list(self._events(state, now))
        if op == KubeOp.POD_LOGS:
            return self._logs(state, now, params)
        raise _KubectlFailure(f"error: unsupported operation {op.value}")  # pragma: no cover

    # --- writes: Kubernetes' rollout rules ----------------------------------------------

    def _write(self, command: Command) -> State:
        params, name = command.params, command.params["name"]
        answer: dict[str, State] = {}

        def change(state: State) -> tuple[State, None]:
            _require(state, name)
            now = self.clock()
            working = copy.deepcopy(state)  # a dry run must leave the cluster as it was
            deployment = working["deployments"][name]
            entry = self.spec()["services"][name]
            image, restarted = _template_key(deployment["spec"]["template"])
            cause = deployment["metadata"]["annotations"].get(CHANGE_CAUSE, "")
            if command.op == KubeOp.ROLLOUT_UNDO:
                target = _revision_rs(working, name, params["revision"])
                if target is None:
                    raise _KubectlFailure(
                        f"error: unable to find specified revision {params['revision']} in history"
                    )
                if _template_key(target["spec"]["template"]) == (image, restarted):
                    raise _KubectlFailure("skipped rollback (current template already matches)")
                image, restarted = _template_key(target["spec"]["template"])
                cause = target["metadata"]["annotations"].get(CHANGE_CAUSE, "")
            elif command.op == KubeOp.ROLLOUT_RESTART:
                restarted = _iso(now)
            else:  # SET_IMAGE: the scenario's typed patch
                image, cause = params["image"], params["change_cause"]
            deployment["spec"]["template"] = _template(name, image, entry, restarted_at=restarted)
            deployment["metadata"]["generation"] += 1
            deployment["metadata"]["annotations"][CHANGE_CAUSE] = cause
            if command.dry_run:
                answer["view"] = self._deployment_view(working, name, now, dry_run=True)
                return state, None
            rs = self._activate(working, name, deployment["spec"]["template"], cause, now)
            deployment["metadata"]["annotations"][REVISION] = rs["metadata"]["annotations"][
                REVISION
            ]
            answer["view"] = self._deployment_view(working, name, now)
            return working, None

        self.state()  # seed first: update() needs a document to change
        self.store.update(change)
        return answer["view"]

    def _activate(
        self, state: State, service: str, template: State, cause: str, now: datetime
    ) -> State:
        """Make `template` current: reuse a ReplicaSet with the same template (renumbered,
        as an undo does) or create one, and start the rollout from the pods serving now."""
        deployment = state["deployments"][service]
        serving = self._rollout(state, service, now)[0]  # before the new template is current
        owned = [rs for rs in state["replicasets"].values() if _owned(rs, deployment)]
        next_number = max((int(rs["metadata"]["annotations"][REVISION]) for rs in owned), default=0)
        next_number += 1
        key = _template_key(template)
        rs = next((r for r in owned if _template_key(r["spec"]["template"]) == key), None)
        if rs is None:
            rs = _replicaset(self.namespace, deployment, template, next_number, cause, now)
            state["replicasets"][_name(rs)] = rs
        else:
            annotations = rs["metadata"]["annotations"]
            history = [h for h in annotations.get(REVISION_HISTORY, "").split(",") if h]
            annotations[REVISION_HISTORY] = ",".join([*history, annotations[REVISION]])
            annotations[REVISION] = str(next_number)
            annotations[CHANGE_CAUSE] = cause
        new = _name(rs)
        image = _image_of_rs(rs)
        if new == serving:
            takes_over: datetime | None = now  # back to the pods that still serve
        elif self._behaviour(image).get("crash"):
            takes_over = None  # never ready: the old pods keep serving
        else:
            state["activations"][new] = _iso(now)
            takes_over = now + timedelta(seconds=self._rollout_seconds())
            state["events"].append(
                _event(
                    deployment,
                    "Normal",
                    "ScalingReplicaSet",
                    f"Scaled up replica set {new} to 1",
                    now,
                )
            )
        # When the new pods take over is fixed now: a later setting never changes this rollout.
        ends = _iso(takes_over) if takes_over is not None else None
        state["rollouts"][service] = {
            "from": serving,
            "to": new,
            "started": _iso(now),
            "ends": ends,
        }
        if takes_over is not None:
            state["timeline"].append({"at": _iso(takes_over), "service": service, "image": image})
            for window in state["pressure"]:  # new pods start with empty memory
                if window["service"] == service and window["until"] is None and window["rs"] != new:
                    window["until"] = _iso(takes_over)
        return rs

    def _rollout_seconds(self) -> float:
        return float(self.spec().get("rollout_seconds", 0))

    # --- derived views ------------------------------------------------------------------

    def _rollout(self, state: State, service: str, now: datetime) -> tuple[str, str, bool]:
        """(serving ReplicaSet, current ReplicaSet, rollout still in progress?) at `now`."""
        current = _current_rs(state, state["deployments"][service])
        rollout = state["rollouts"].get(service)
        if rollout is None or rollout["from"] == current:
            return current, current, False
        ends = rollout.get("ends")  # None: the new pods never take over (they crash)
        if ends is None or now < _time(ends):
            return rollout["from"], current, True
        return current, current, False

    def _behaviour(self, image: str | None) -> State:
        return dict(self.spec().get("images", {}).get(image or "", {}))

    def _pressure(self, state: State, service: str, at: datetime) -> bool:
        """Was the service's memory full (readiness refusing) at `at`?"""
        return any(
            window["service"] == service
            and _time(window["from"]) <= at
            and (window["until"] is None or at < _time(window["until"]))
            for window in state["pressure"]
        )

    def _pods(self, state: State, service: str, now: datetime) -> list[State]:
        replicas = state["deployments"][service]["spec"]["replicas"]
        serving, current, in_progress = self._rollout(state, service, now)
        full = self._pressure(state, service, now)
        # A crashing image serves only when it was seeded as the current release: an external
        # harness rolled it out with no readiness probe, so the old pods are gone and its own
        # pods crash-loop. Through a write it never takes over (the rollout stays stuck).
        rs = state["replicasets"][serving]
        broken = bool(self._behaviour(_image_of_rs(rs)).get("crash"))
        pods = [self._pod(state, rs, i, now, ready=not full, crash=broken) for i in range(replicas)]
        if in_progress:
            rs = state["replicasets"][current]
            crash = bool(self._behaviour(_image_of_rs(rs)).get("crash"))
            pods.append(self._pod(state, rs, 0, now, ready=False, crash=crash))
        return pods

    def _ready_pods(self, state: State, service: str, now: datetime) -> int:
        return sum(1 for pod in self._pods(state, service, now) if _ready(pod))

    def _pod(
        self, state: State, rs: State, index: int, now: datetime, *, ready: bool, crash: bool
    ) -> State:
        """A pod of `rs`. A crashing one is not ready, except while a start of an image with
        `crash_after_seconds` runs: these Deployments have no readiness probe."""
        rs_name = _name(rs)
        activated = state["activations"].get(rs_name) or rs["metadata"]["creationTimestamp"]
        suffix = _digest(rs_name, activated, index, length=5)
        container = rs["spec"]["template"]["spec"]["containers"][0]
        started = _time(activated)
        last: State = {}
        restarts = 0
        current: State = {"running": {"startedAt": _iso(started)}}
        if crash:
            current, last, restarts = self._crash_loop(rs, started, now)
            ready = ready and "running" in current
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{rs_name}-{suffix}",
                "namespace": self.namespace,
                "uid": f"uid-pod-{_digest(rs_name, activated, index)}",
                "creationTimestamp": _iso(started),
                "labels": dict(rs["spec"]["template"]["metadata"]["labels"]),
                "ownerReferences": [
                    {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": rs_name}
                ],
            },
            "spec": {"containers": [container]},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                "containerStatuses": [
                    {
                        "name": container["name"],
                        "image": container["image"],
                        "ready": ready,
                        "restartCount": restarts,
                        "state": current,
                        "lastState": last,
                    }
                ],
                "startTime": _iso(started),
            },
        }

    def _crash_loop(self, rs: State, started: datetime, now: datetime) -> tuple[State, State, int]:
        """A crash-looping container's (state, lastState, restartCount) at `now`. By default
        it is waiting to restart. An image with `crash_after_seconds` runs that long after each
        start (a dependency dial that times out) and waits CRASH_BACKOFF_SECONDS between starts."""
        lasts = self._behaviour(_image_of_rs(rs)).get("crash_after_seconds")
        waiting = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}}
        if lasts is None:
            return waiting, _terminated(started, started + timedelta(seconds=2)), 4
        run = timedelta(seconds=float(lasts))
        cycle = run + timedelta(seconds=CRASH_BACKOFF_SECONDS)
        starts = max(int((now - started) / cycle), 0)  # completed cycles before `now`
        latest = started + starts * cycle
        if now - latest < run:  # this start still runs
            previous = _terminated(latest - cycle, latest - cycle + run) if starts else {}
            return {"running": {"startedAt": _iso(latest)}}, previous, starts
        return waiting, _terminated(latest, latest + run), starts + 1

    def _deployment_view(
        self, state: State, service: str, now: datetime, *, dry_run: bool = False
    ) -> State:
        """The Deployment with the status the controller would report at `now`."""
        deployment = copy.deepcopy(state["deployments"][service])
        replicas = deployment["spec"]["replicas"]
        generation = deployment["metadata"]["generation"]
        if dry_run:  # the API server's answer to a dry run: never observed by a controller
            deployment["status"] = {"observedGeneration": generation - 1}
            return deployment
        _, current, in_progress = self._rollout(state, service, now)
        crashes = in_progress and bool(
            self._behaviour(_image_of_rs(state["replicasets"][current])).get("crash")
        )
        ready = self._ready_pods(state, service, now)
        available = min(ready, replicas)
        if crashes:
            progressing = {
                "type": "Progressing",
                "status": "False",
                "reason": "ProgressDeadlineExceeded",
            }
        else:
            reason = "ReplicaSetUpdated" if in_progress else "NewReplicaSetAvailable"
            progressing = {"type": "Progressing", "status": "True", "reason": reason}
        surge = 1 if in_progress else 0
        deployment["status"] = {
            "observedGeneration": generation,
            "replicas": replicas + surge,
            "updatedReplicas": 1 if in_progress else replicas,
            "readyReplicas": ready,
            "availableReplicas": available,
            "unavailableReplicas": replicas - available + surge,
            "conditions": [
                {"type": "Available", "status": "True" if available >= replicas else "False"},
                progressing,
            ],
        }
        return deployment

    def _events(self, state: State, now: datetime) -> list[State]:
        events = list(state["events"])
        for service in sorted(state["deployments"]):
            for pod in self._pods(state, service, now):
                status = pod["status"]["containerStatuses"][0]
                if status["state"].get("waiting", {}).get("reason") == "CrashLoopBackOff":
                    message = (
                        f"Back-off restarting failed container {status['name']} in pod {_name(pod)}"
                    )
                    events.append(_event(pod, "Warning", "BackOff", message, now, count=5))
                elif not _ready(pod):
                    message = "Readiness probe failed: HTTP probe failed with statuscode: 503"
                    events.append(_event(pod, "Warning", "Unhealthy", message, now, count=6))
        return events

    # --- logs ---------------------------------------------------------------------------

    def _logs(self, state: State, now: datetime, params: Mapping[str, Any]) -> str:
        found = [
            (service, pod)
            for service in state["deployments"]
            for pod in self._pods(state, service, now)
            if _name(pod) == params["pod"]
        ]
        if not found:
            raise _KubectlFailure(f'Error from server (NotFound): pods "{params["pod"]}" not found')
        service, pod = found[0]
        image = pod["status"]["containerStatuses"][0]["image"]
        behaviour = self._behaviour(image)
        version = version_of(image) or "unknown"
        started = _time(pod["metadata"]["creationTimestamp"])
        if behaviour.get("crash"):
            lines = [
                (started, "INFO", f"starting [build {version}]"),
                (started + timedelta(seconds=1), "ERROR", str(behaviour.get("error_log", "fatal"))),
            ]
        elif params.get("previous"):
            raise _KubectlFailure(
                "Error from server (BadRequest): previous terminated container "
                f'"{params["container"]}" in pod "{params["pod"]}" not found'
            )
        else:
            lines = self._app_lines(state, service, started, behaviour, version, now)
        since = _time(params["since_time"])
        kept = [line for line in lines if line[0] >= since][-int(params["tail"]) :]
        prefix = f"[pod/{params['pod']}/{params['container']}]"
        text = "".join(
            f"{prefix} {_nano(at)} {_milli(at)} {level} {message}\n" for at, level, message in kept
        )
        return text.encode()[: int(params["limit_bytes"])].decode(errors="ignore")

    def _app_lines(
        self,
        state: State,
        service: str,
        started: datetime,
        behaviour: State,
        version: str,
        now: datetime,
    ) -> list[tuple[datetime, str, str]]:
        """The start-up line, then a request line every LOG_INTERVAL_SECONDS back from `now`,
        each from the app's behaviour AT THAT MOMENT (lines before a fault look calm)."""
        horizon = max(started, now - timedelta(minutes=LOG_HORIZON_MINUTES))
        limit = self.spec()["services"][service].get("memory_limit", "128Mi")
        percent = round(100 * float(self.spec().get("memory_high_ratio", 0.95)))
        newest_first: list[tuple[datetime, str, str]] = []
        index, moment = 0, now
        while moment > horizon:
            rate, upstream = self._error_rate(state, service, moment)
            if self._pressure(state, service, moment):
                message = f"memory pressure: {percent}% of the container limit ({limit}) in use"
                newest_first.append((moment, "WARN", message))
            elif _fails(index, rate):
                error = upstream or str(behaviour.get("error_log", "error"))
                newest_first.append((moment, "ERROR", error))
            else:
                newest_first.append((moment, "INFO", str(behaviour.get("log", "request served"))))
            index += 1
            moment -= timedelta(seconds=LOG_INTERVAL_SECONDS)
        startup = (started, "INFO", f"serving on :8080 [build {version}]")
        return [startup, *reversed(newest_first)]

    # --- the app's behaviour over time ----------------------------------------------------

    def _image_at(self, state: State, service: str, at: datetime) -> str | None:
        """The image whose pods served `service` at `at` (None before it existed)."""
        image = None
        for entry in state["timeline"]:
            if entry["service"] == service and _time(entry["at"]) <= at:
                image = entry["image"]
        return image

    def _unavailable(self, state: State, service: str, at: datetime) -> bool:
        """No ready pod at `at`: memory full, or (now) nothing ready at all."""
        if self._pressure(state, service, at):
            return True
        now = self.clock()
        return at >= now - _NOW_SLACK and self._ready_pods(state, service, now) == 0

    def _dependencies(self, state: State, service: str) -> list[str]:
        raw = state["deployments"][service]["metadata"]["annotations"].get(DEPENDS_ON, "")
        return [d for d in (part.strip() for part in raw.split(",")) if d in state["deployments"]]

    def _error_rate(self, state: State, service: str, at: datetime) -> tuple[float, str]:
        """(share of failing requests at `at`, the upstream error line if a dependency is why)."""
        if self._unavailable(state, service, at):
            return 1.0, ""
        own = float(self._behaviour(self._image_at(state, service, at)).get("error_rate", 0.0))
        lines = self.spec().get("upstream_errors", {}).get(service, {})
        for dependency in self._dependencies(state, service):
            if self._unavailable(state, dependency, at):
                line = lines.get("unavailable", f"rpc to {dependency} failed: UNAVAILABLE")
                return 1.0, str(line)
            failing, _ = self._error_rate(state, dependency, at)
            if failing > own:
                return failing, str(lines.get("failing", f"rpc to {dependency} failed: INTERNAL"))
        return own, ""

    def _metric_value(self, state: State, metric: str, service: str, at: datetime) -> float | None:
        """What Prometheus would hold at `at`: None before the service existed, NaN where
        nothing could be measured (no request reaches a service without ready pods)."""
        image = self._image_at(state, service, at)
        if image is None:
            return None
        behaviour = self._behaviour(image)
        if metric == "error_rate":
            if self._unavailable(state, service, at) and not self._dependencies(state, service):
                return math.nan
            return self._error_rate(state, service, at)[0]
        if metric == "latency_p95_ms":
            return float(behaviour.get("latency_ms", 50))
        if metric == "memory_mb":
            if self._pressure(state, service, at):
                entry = self.spec()["services"][service]
                limit = (parse_quantity(entry.get("memory_limit", "128Mi")) or 0) / 1048576
                return limit * float(self.spec().get("memory_high_ratio", 0.95))
            return float(behaviour.get("memory_mb", 40))
        if metric == "cpu_pct":
            return float(behaviour.get("cpu_cores", 0.01))
        return None  # pragma: no cover - the query matched one of the four templates

    # --- HTTP: Prometheus and the services' NodePorts -----------------------------------

    def http_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._http)

    def _http(self, request: httpx.Request) -> httpx.Response:
        host = request.url.netloc.decode()
        if host == self.prometheus:
            return self._prometheus(request)
        service = self.hosts.get(host)
        if service is None:
            raise httpx.ConnectError("connection refused", request=request)
        return self._serve(service, request)

    def _serve(self, service: str, request: httpx.Request) -> httpx.Response:
        state, now = self.state(), self.clock()
        if self._ready_pods(state, service, now) == 0:
            raise httpx.ConnectError("connection refused", request=request)
        burst = self.spec().get("report_burst", {})
        if service == burst.get("service") and request.url.path == burst.get("path", "/report"):
            self._report_counts[service] = self._report_counts.get(service, 0) + 1
            if self._report_counts[service] == int(burst.get("fills_at", 100)):
                self._fill_memory(service)
            return httpx.Response(200, json={"report": "ok"})
        rate, _ = self._error_rate(state, service, now)
        count = self._probe_counts.get(service, 0)
        self._probe_counts[service] = count + 1
        if _fails(count, rate):
            return httpx.Response(500, json={"error": "internal"})
        return httpx.Response(200, json={"ok": True})

    def _fill_memory(self, service: str) -> None:
        def change(state: State) -> tuple[State, None]:
            serving = self._rollout(state, service, self.clock())[0]
            window = {"service": service, "rs": serving, "from": _iso(self.clock()), "until": None}
            return {**state, "pressure": [*state["pressure"], window]}, None

        self.state()
        self.store.update(change)

    def _prometheus(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = params.get("query", "")
        if query == "time()":
            stamp = self.clock().timestamp()
            return _ok({"resultType": "scalar", "result": [stamp, f"{stamp:.3f}"]})
        found = self._match_query(query)
        if found is None:
            return httpx.Response(400, json={"status": "error", "error": "unknown query"})
        metric, service = found
        state = self.state()
        if request.url.path.endswith("/query_range"):
            start, end, step = float(params["start"]), float(params["end"]), float(params["step"])
            values = []
            moment = start
            while moment <= end + 1e-6:
                when = datetime.fromtimestamp(moment, UTC)
                value = self._metric_value(state, metric, service, when)
                if value is not None:
                    values.append([moment, _sample(value)])
                moment += step
            series = [{"metric": {}, "values": values}] if values else []
            return _ok({"resultType": "matrix", "result": series})
        stamp = float(params.get("time", self.clock().timestamp()))
        value = self._metric_value(state, metric, service, datetime.fromtimestamp(stamp, UTC))
        sample = [{"metric": {}, "value": [stamp, _sample(value)]}] if value is not None else []
        return _ok({"resultType": "vector", "result": sample})

    def _match_query(self, query: str) -> tuple[str, str] | None:
        found = _WINDOW.search(query)
        window = found.group(1) if found else "1m"
        for metric in PROMETHEUS_METRICS:
            for service in self.spec()["services"]:
                if promql(metric, self.namespace, service, window) == query:
                    return metric, service
        return None


class FakeKubeClient(KubeClient):
    """The typed operations over the fake cluster: the same validation, the same identity
    checks and the same strict parsing as KubectlClient; only the execution differs."""

    def __init__(self, cluster: FakeCluster, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cluster = cluster

    async def execute(self, command: Command) -> tuple[int, str, str]:
        return self.cluster.handle(command, self.identity)


# --- object builders (shapes from the Kubernetes API reference) -------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _terminated(started: datetime, finished: datetime) -> State:
    return {
        "terminated": {
            "reason": "Error",
            "exitCode": 1,
            "startedAt": _iso(started),
            "finishedAt": _iso(finished),
        }
    }


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _digest(*parts: object, length: int = 10) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:length]


def _name(obj: Mapping[str, Any]) -> str:
    return str(obj["metadata"]["name"])


def _ready(pod: Mapping[str, Any]) -> bool:
    return bool(pod["status"]["conditions"][0]["status"] == "True")


def _require(state: State, name: str) -> None:
    if name not in state["deployments"]:
        raise _KubectlFailure(f'Error from server (NotFound): deployments.apps "{name}" not found')


def _version(text: str) -> State:
    major, minor = text.lstrip("v").split(".")[:2]
    return {"major": major, "minor": minor, "gitVersion": text, "platform": "fake/offline"}


def _list(items: Sequence[State]) -> State:
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": list(items),
        "metadata": {"resourceVersion": ""},
    }


def _labels(selector: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in selector.split(","))


def _match(obj: Mapping[str, Any], wanted: Mapping[str, str]) -> bool:
    labels = obj["metadata"].get("labels", {})
    return all(labels.get(key) == value for key, value in wanted.items())


def _owned(rs: Mapping[str, Any], deployment: Mapping[str, Any]) -> bool:
    uid = deployment["metadata"]["uid"]
    return any(owner.get("uid") == uid for owner in rs["metadata"].get("ownerReferences", []))


def _template(
    service: str, image: str, entry: Mapping[str, Any], *, restarted_at: str | None
) -> State:
    annotations = {RESTARTED_AT: restarted_at} if restarted_at else {}
    limits = {"memory": entry.get("memory_limit", "128Mi"), "cpu": entry.get("cpu_limit", "250m")}
    return {
        "metadata": {"labels": {"app": service}, "annotations": annotations},
        "spec": {
            "containers": [
                {
                    "name": service,
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "ports": [{"containerPort": 8080}],
                    "resources": {"limits": limits, "requests": dict(limits)},
                    "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                }
            ]
        },
    }


def _template_key(template: Mapping[str, Any]) -> tuple[str, str | None]:
    """What makes two templates equal here: the image and the restart annotation."""
    container = template["spec"]["containers"][0]
    annotations = template.get("metadata", {}).get("annotations", {})
    return str(container["image"]), annotations.get(RESTARTED_AT)


def _deployment(namespace: str, service: str, entry: Mapping[str, Any], born: datetime) -> State:
    annotations = {CHANGE_CAUSE: f"release {version_of(entry['image']) or 'unknown'}"}
    if entry.get("depends_on"):
        annotations[DEPENDS_ON] = str(entry["depends_on"])
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": service,
            "namespace": namespace,
            "uid": f"uid-deploy-{service}",
            "generation": 1,
            "creationTimestamp": _iso(born),
            "labels": {"app": service},
            "annotations": annotations,
        },
        "spec": {
            "replicas": int(entry.get("replicas", 2)),
            "selector": {"matchLabels": {"app": service}},
            "progressDeadlineSeconds": 60,
            "revisionHistoryLimit": 10,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
            "template": _template(service, entry["image"], entry, restarted_at=None),
        },
    }


def _replicaset(
    namespace: str, deployment: State, template: State, number: int, cause: str, created: datetime
) -> State:
    service = _name(deployment)
    pod_hash = _digest(json.dumps(template, sort_keys=True))
    labeled = copy.deepcopy(template)
    labeled["metadata"]["labels"]["pod-template-hash"] = pod_hash
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": f"{service}-{pod_hash}",
            "namespace": namespace,
            "uid": f"uid-rs-{pod_hash}",
            "creationTimestamp": _iso(created),
            "labels": {"app": service, "pod-template-hash": pod_hash},
            "annotations": {REVISION: str(number), CHANGE_CAUSE: cause},
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": service,
                    "uid": deployment["metadata"]["uid"],
                    "controller": True,
                }
            ],
        },
        "spec": {
            "replicas": deployment["spec"]["replicas"],
            "selector": {"matchLabels": {"app": service, "pod-template-hash": pod_hash}},
            "template": labeled,
        },
    }


def _current_rs(state: State, deployment: State) -> str:
    owned = [rs for rs in state["replicasets"].values() if _owned(rs, deployment)]
    newest = max(owned, key=lambda rs: int(rs["metadata"]["annotations"][REVISION]))
    return _name(newest)


def _revision_rs(state: State, service: str, number: int) -> State | None:
    deployment = state["deployments"][service]
    for rs in state["replicasets"].values():
        if _owned(rs, deployment) and rs["metadata"]["annotations"][REVISION] == str(number):
            return rs
    return None


def _image_of_rs(rs: Mapping[str, Any]) -> str:
    return str(rs["spec"]["template"]["spec"]["containers"][0]["image"])


def _event(
    obj: Mapping[str, Any], kind_type: str, reason: str, message: str, at: datetime, count: int = 1
) -> State:
    name = _name(obj)
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {"name": f"{name}.{_digest(reason, message, _iso(at), length=12)}"},
        "involvedObject": {"kind": obj["kind"], "name": name},
        "reason": reason,
        "message": message,
        "type": kind_type,
        "count": count,
        "firstTimestamp": _iso(at),
        "lastTimestamp": _iso(at),
    }


def _fails(index: int, rate: float) -> bool:
    """Spread failures evenly: request k fails when the running share reaches the next whole
    number (so the first request already fails when the rate is above zero)."""
    return math.ceil((index + 1) * rate - 1e-9) > math.ceil(index * rate - 1e-9)


def _sample(value: float) -> str:
    return "NaN" if math.isnan(value) else f"{value:.6f}"


def _ok(data: State) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def _nano(at: datetime) -> str:
    """The kubelet's --timestamps prefix: RFC 3339 with nanoseconds."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond:06d}000Z"


def _milli(at: datetime) -> str:
    """The demo app's own line time: RFC 3339 UTC with milliseconds."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"
