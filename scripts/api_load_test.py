"""A small, reproducible load test of the HTTP API, in process: N incident runs at once.

    python scripts/api_load_test.py                        # 8 runs at once
    python scripts/api_load_test.py --runs 3 --scenario bad-deployment --timeout 120
    RECAP_TOOL_LATENCY_MS=0 python scripts/api_load_test.py   # without simulated tool latency

What it does: it builds the API around a fresh Runtime in a temporary data directory and
talks to it through httpx's ASGI transport: no server, no port, no network. N simulated
operators each start one run of the same scenario at the same moment, with the scripted
baseline (deterministic, not a model). Each waits for its own run's approval request in GET
/approvals/pending, approves that exact proposal with its digest through POST
/approvals/{proposal_id}, as an operator does from the dashboard, and waits for its run to
finish. Memory is off for these runs, so no run's path depends on what another learned first.

It prints one JSON report: each run's final state, its recorded duration (creation to final
state) and whether its record and its one approval are its own; the latency percentiles
(p50, p95, p99, max) of every request, overall and by route template; the count of 5xx
answers; and what GET /metrics counted on the server side (RECAP_METRICS=1 is set for the app
it builds). Exit code 0 means every run was RESOLVED with its own record and no request got a
5xx; 1 means some did not; 2 means the test refused to run.

What the numbers are: one machine, one process, the SIMULATED environment and the scripted
baseline. They show that concurrent runs and their approvals stay independent, and how the
API's latency behaves around them. They are not a capacity figure for any deployment. The
test refuses to run against anything but the simulated environment.

Against a running server (M6 W6), --url sends the same operators over real HTTP to an API that
is already up, with whatever execution backend it uses (in Docker Compose: the Redis job queue
and separate worker processes), at one or more offered loads in turn:

    RECAP_OPERATOR_TOKEN=... python scripts/api_load_test.py --url http://127.0.0.1:8001 \
        --loads 4,16,40 [--docker-stats] [--out report.json]

`--loads 4,16,40` runs three levels, one after another: 4, then 16, then 40 operators at once.
Each level reports its throughput (runs finished per second of the level's wall time), the HTTP
latency percentiles, each run's duration and its queue wait (the job's submission to its first
claim, from GET /runs/{run_id}), the submissions the queue refused (429: its backpressure, not an
error) and every 5xx. --docker-stats samples `docker stats` while each level runs (the peak CPU
and memory of each container). The report names the machine. The server must say its target is
simulated (GET /health), or nothing starts; runs use the scripted baseline, never a model. These
are one machine's measurements of one setup, not a capacity figure for any deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running without install

import httpx
from fastapi import FastAPI

from apps.api.auth import OperatorAuth
from apps.api.main import create_app
from recap.core.config import Settings, load_settings
from recap.runtime import Runtime

DEFAULT_RUNS = 8
DEFAULT_SCENARIO = "bad-deployment"
DEFAULT_TIMEOUT = 180.0  # seconds the whole test may take
POLL_SECONDS = 0.1  # how often an operator looks again (the pending list, its run)
BASE_URL = "http://localhost"  # the API serves loopback host names only
APPLIED = ("recovered", "not_recovered")  # action results of a write the target applied
IN_PROGRESS = ("running", "queued")  # a run that has not ended (queued: waiting for a worker)
# One sample line of GET /metrics, and one label in it (this app's own output, nothing else).
SAMPLE = re.compile(r"(?P<name>[a-z_]+)(?:\{(?P<labels>.*)\})? (?P<value>\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
# The samples server_metrics reads: the counters and the active-incident gauge.
COUNTED = frozenset(
    {
        "recap_api_requests_total",
        "recap_active_incidents",
        "recap_incidents_finished_total",
        "recap_actions_total",
        "recap_recovery_verifications_total",
        "recap_tool_calls_total",
        "recap_model_requests_total",
    }
)


class LoadTestRefused(Exception):
    """The app is not one this load test may run against (its target is not simulated)."""


class TimedClient:
    """Sends the operators' requests and times each one, by method and route template."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.latency_ms: dict[str, list[float]] = defaultdict(list)
        self.errors_5xx = 0

    async def send(
        self, method: str, route: str, path: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        """Send one request to `path` (by default the route itself) and time it."""
        started = time.perf_counter()
        response = await self.client.request(method, path or route, **kwargs)
        self.latency_ms[f"{method} {route}"].append((time.perf_counter() - started) * 1000)
        if response.status_code >= 500:
            self.errors_5xx += 1
        return response


async def load_test(
    app: FastAPI,
    *,
    runs: int,
    token: str,
    scenario: str = DEFAULT_SCENARIO,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Start `runs` incidents at once through `app`, approve each like an operator, report.

    Refuses (LoadTestRefused) before starting anything unless the app's target is the
    simulated environment.
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with app.router.lifespan_context(app):
        runtime: Runtime = app.state.manager.runtime
        if not runtime.environment_info().is_simulated:
            raise LoadTestRefused(
                "this API's target is not the simulated environment: the load test never "
                "starts runs on a live target"
            )
        provider = scripted_provider(runtime.settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=headers, timeout=timeout
        ) as client:
            timed = TimedClient(client)
            deadline = time.monotonic() + timeout
            started = time.perf_counter()
            results = await asyncio.gather(
                *(operate(timed, scenario, provider, deadline) for _ in range(runs))
            )
            wall = time.perf_counter() - started
            metrics = await client.get("/metrics")  # not timed: it is not part of the load
    return report(list(results), timed, scenario, provider, wall, metrics)


async def load_test_url(
    url: str,
    *,
    loads: list[int],
    token: str,
    provider: str,
    scenario: str = DEFAULT_SCENARIO,
    timeout: float = DEFAULT_TIMEOUT,
    docker_stats: bool = False,
) -> dict[str, Any]:
    """The operators against the running server at `url`, one offered load after another.
    Refuses (LoadTestRefused) before starting anything unless the server's target is the
    simulated environment."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=timeout) as client:
        health = await client.get("/health")
        target = health.json().get("environment") if health.status_code == 200 else None
        if target != "simulated":
            raise LoadTestRefused(
                f"the server at {url} says its target is {target!r}, not the simulated "
                "environment: the load test never starts runs on a live target"
            )
        levels = []
        for offered in loads:
            timed = TimedClient(client)
            sampler = DockerStats() if docker_stats else None
            if sampler is not None:
                sampler.start()
            deadline = time.monotonic() + timeout
            started = time.perf_counter()
            runs = await asyncio.gather(
                *(operate(timed, scenario, provider, deadline) for _ in range(offered))
            )
            wall = time.perf_counter() - started
            peaks = sampler.stop() if sampler is not None else None
            levels.append(level_report(offered, list(runs), timed, wall, peaks))
        metrics = await client.get("/metrics")  # not timed: they are not part of the load
        queue = (await client.get("/queue")).json()
    return {
        "ok": all(level["ok"] for level in levels),
        "what": "HTTP load test against a running API: the SIMULATED environment and the "
        "scripted baseline (not a model), one machine's measurements of one setup, not a "
        "capacity figure",
        "server": url,
        "scenario": scenario,
        "provider": provider,
        "machine": machine(docker_stats),
        "execution": {
            "backend": queue.get("backend"),
            "queue_capacity": queue.get("capacity"),
            "workers": len(queue.get("workers") or []),
        },
        "levels": levels,
        "server_metrics": server_metrics(metrics),
    }


def level_report(
    offered: int,
    runs: list[dict[str, Any]],
    timed: TimedClient,
    wall: float,
    containers: dict[str, dict[str, float]] | None,
) -> dict[str, Any]:
    """One offered load: what was accepted, refused and finished, and how long it all took."""
    accepted = [run for run in runs if "refused" not in run]
    refused = [int(run["refused"]) for run in runs if "refused" in run]
    states: dict[str, int] = {}
    for run in accepted:
        states[str(run["state"])] = states.get(str(run["state"]), 0) + 1
    finished = [run for run in accepted if run["state"] is not None]
    latencies = [ms for values in timed.latency_ms.values() for ms in values]
    resolved = all(run["state"] == "RESOLVED" and run["own_record"] for run in accepted)
    return {
        "offered": offered,
        # Backpressure (429: the queue is full) is the queue working, not an error.
        "ok": bool(accepted)
        and resolved
        and timed.errors_5xx == 0
        and all(code == 429 for code in refused),
        "accepted": len(accepted),
        "refused": {str(code): refused.count(code) for code in sorted(set(refused))},
        "states": states,
        "wall_seconds": round(wall, 3),
        "throughput_runs_per_second": round(len(finished) / wall, 3) if wall > 0 else None,
        "run_seconds": percentiles([run["seconds"] for run in finished if run["seconds"]]),
        "queue_wait_seconds": percentiles(
            [run["queue_seconds"] for run in finished if run["queue_seconds"] is not None]
        ),
        "client_seconds": percentiles([run["client_seconds"] for run in accepted]),
        "requests": {
            "total": len(latencies),
            "errors_5xx": timed.errors_5xx,
            "latency_ms": percentiles(latencies),
            "by_route": {
                route: {"count": len(values), **percentiles(values)}
                for route, values in sorted(timed.latency_ms.items())
            },
        },
        "containers": containers,
        "runs": runs,
    }


class DockerStats:
    """`docker stats`, sampled in a thread while a level runs: each container's peak CPU (in
    percent of one core) and peak memory (MiB)."""

    def __init__(self, every: float = 1.0) -> None:
        self.every = every
        self.peaks: dict[str, dict[str, float]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="docker-stats", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, dict[str, float]]:
        self._stop.set()
        self._thread.join(timeout=30)
        return self.peaks

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.every)

    def sample(self) -> None:
        command = [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
        ]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
        except (OSError, subprocess.SubprocessError):
            return
        for line in done.stdout.splitlines():
            name, cpu, memory = (line.split("\t") + ["", ""])[:3]
            peak = self.peaks.setdefault(name, {"cpu_percent": 0.0, "memory_mib": 0.0})
            peak["cpu_percent"] = max(peak["cpu_percent"], _number(cpu.rstrip("%")))
            peak["memory_mib"] = max(peak["memory_mib"], _mib(memory.split("/")[0]))


UNITS = {"B": 1 / 2**20, "KIB": 1 / 1024, "MIB": 1.0, "GIB": 1024.0, "KB": 1 / 1024, "MB": 1.0}


def _mib(text: str) -> float:
    """A `docker stats` size such as 123.4MiB or 1.2GiB, in MiB (0 when unreadable)."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", text)
    if match is None:
        return 0.0
    return round(_number(match[1]) * UNITS.get(match[2].upper(), 0.0), 1)


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def machine(docker: bool) -> dict[str, Any]:
    """The machine the numbers come from: this host and, with Docker, the Docker server."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "memory_gib": _memory_gib(),
    }
    if docker:
        try:
            done = subprocess.run(
                ["docker", "info", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            server = json.loads(done.stdout)
            info["docker"] = {
                "cpus": server.get("NCPU"),
                "memory_gib": round(int(server.get("MemTotal") or 0) / 2**30, 1),
                "server_version": server.get("ServerVersion"),
                "architecture": server.get("Architecture"),
                "os": server.get("OperatingSystem"),
            }
        except (OSError, subprocess.SubprocessError, ValueError):
            info["docker"] = None
    return info


def _memory_gib() -> float | None:
    try:
        if sys.platform == "darwin":
            done = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            return round(int(done.stdout.strip()) / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def scripted_provider(settings: Settings) -> str:
    """The configured scripted baseline: whatever the server's default is, no model is used."""
    for name, spec in settings.providers.providers.items():
        if spec.origin == "scripted":
            return name
    raise LoadTestRefused("no scripted provider is configured (configs/providers.yaml)")


async def operate(
    timed: TimedClient, scenario: str, provider: str, deadline: float
) -> dict[str, Any]:
    """One operator: start a run, approve its exact proposal, wait for it to finish."""
    config = {
        "provider": provider,
        "approval_mode": "manual",  # a person decides, here through POST /approvals
        "use_memory": False,
        "memory_write": False,
    }
    started = time.perf_counter()
    answer = await timed.send(
        "POST", "/incidents/run", json={"scenario_id": scenario, "config": config}
    )
    if answer.status_code != 202:
        error = f"POST /incidents/run answered {answer.status_code}: {answer.text[:200]}"
        refused = describe_run(None, None, None, time.perf_counter() - started, error)
        refused["refused"] = answer.status_code  # 429: the queue is full (backpressure)
        return refused
    run_id = answer.json()["run_id"]
    approved = None
    pending = await own_request(timed, run_id, deadline)
    if pending is not None:
        decision = {"decision": "approved", "proposal_digest": pending["proposal_digest"]}
        path = f"/approvals/{pending['proposal_id']}"
        decided = await timed.send("POST", "/approvals/{proposal_id}", path, json=decision)
        if decided.status_code == 200:
            approved = pending["proposal_id"]
    body = await finished(timed, run_id, deadline)
    failed = body.get("error") if body else "timed out (the run was cancelled)"
    return describe_run(run_id, body, approved, time.perf_counter() - started, failed)


async def own_request(timed: TimedClient, run_id: str, deadline: float) -> dict[str, Any] | None:
    """This run's approval request as the pending list shows it; None when the run ended
    without one or the time is up."""
    while time.monotonic() < deadline:
        pending = (await timed.send("GET", "/approvals/pending")).json()
        mine = [request for request in pending if request["run_id"] == run_id]
        if mine:
            return mine[0]
        run = await timed.send("GET", "/runs/{run_id}", f"/runs/{run_id}")
        if run.json()["status"] not in IN_PROGRESS:
            return None
        await asyncio.sleep(POLL_SECONDS)
    return None


async def finished(timed: TimedClient, run_id: str, deadline: float) -> dict[str, Any] | None:
    """GET /runs/{run_id} once the run has finished; None, with the run cancelled, when the
    time is up first."""
    while time.monotonic() < deadline:
        run = await timed.send("GET", "/runs/{run_id}", f"/runs/{run_id}")
        body: dict[str, Any] = run.json()
        if body["status"] not in IN_PROGRESS and (body["record"] or body["error"]):
            return body
        await asyncio.sleep(POLL_SECONDS)
    await timed.send("POST", "/runs/{run_id}/cancel", f"/runs/{run_id}/cancel")
    return None


def describe_run(
    run_id: str | None,
    body: dict[str, Any] | None,
    approved: str | None,
    client_seconds: float,
    error: str | None,
) -> dict[str, Any]:
    """One run's entry in the report, from what GET /runs/{run_id} returned."""
    record: dict[str, Any] = (body or {}).get("record") or {}
    history = record.get("history") or []
    seconds = None
    if len(history) >= 2:  # from creation to the final state, as the run recorded them
        created = datetime.fromisoformat(history[0]["at"])
        ended = datetime.fromisoformat(history[-1]["at"])
        seconds = round((ended - created).total_seconds(), 3)
    approvals = [approval["proposal_id"] for approval in record.get("approvals", [])]
    writes = [action for action in record.get("actions", []) if action["result"] in APPLIED]
    # Its record is its own: the same run id, and the only approval in it is the one this
    # operator gave for this run's proposal (approvals never cross between runs).
    own = run_id is not None and record.get("run_id") == run_id
    job = (body or {}).get("job") or {}  # the Redis backend's job: its queue wait
    queued = None
    if job.get("submitted_at") and job.get("claimed_at"):
        submitted = datetime.fromisoformat(job["submitted_at"])
        queued = round((datetime.fromisoformat(job["claimed_at"]) - submitted).total_seconds(), 3)
    return {
        "run_id": run_id,
        "state": record.get("state"),
        "seconds": seconds,
        "queue_seconds": queued,
        "client_seconds": round(client_seconds, 3),  # from the POST to seeing it finished
        "approved_proposal": approved,
        "own_record": own and approved is not None and approvals == [approved],
        "writes": len(writes),
        "error": error,
    }


def report(
    runs: list[dict[str, Any]],
    timed: TimedClient,
    scenario: str,
    provider: str,
    wall: float,
    metrics: httpx.Response,
) -> dict[str, Any]:
    """The JSON report: the runs, the request latencies and what the server counted."""
    states: dict[str, int] = {}
    for run in runs:
        states[str(run["state"])] = states.get(str(run["state"]), 0) + 1
    latencies = [ms for values in timed.latency_ms.values() for ms in values]
    durations = [run["seconds"] for run in runs if run["seconds"] is not None]
    resolved = all(run["state"] == "RESOLVED" and run["own_record"] for run in runs)
    return {
        "ok": bool(runs) and resolved and timed.errors_5xx == 0,
        "what": "in-process load test: SIMULATED environment, scripted baseline (not a model), "
        "httpx ASGI transport (no server, no network); not a capacity figure",
        "scenario": scenario,
        "provider": provider,
        "runs_started": len(runs),
        "states": states,
        "run_seconds": percentiles(durations),
        "wall_seconds": round(wall, 3),
        "requests": {
            "total": len(latencies),
            "errors_5xx": timed.errors_5xx,
            "latency_ms": percentiles(latencies),
            "by_route": {
                route: {"count": len(values), **percentiles(values)}
                for route, values in sorted(timed.latency_ms.items())
            },
        },
        "server_metrics": server_metrics(metrics),
        "runs": runs,
    }


def percentiles(values: list[float]) -> dict[str, float]:
    """p50, p95, p99 and max of `values` (nearest rank); empty when there are none."""
    if not values:
        return {}
    ordered = sorted(values)

    def rank(share: float) -> float:
        return ordered[max(0, math.ceil(share * len(ordered)) - 1)]

    return {
        name: round(rank(share), 3)
        for name, share in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1.0))
    }


def server_metrics(response: httpx.Response) -> dict[str, Any] | None:
    """What GET /metrics counted (None when the app's metrics are off: RECAP_METRICS)."""
    if response.status_code != 200:
        return None
    found: dict[str, Any] = {
        "api_requests": 0,
        "api_5xx": 0,
        "active_incidents": 0,
        "incidents_finished": {},
        "actions": {},
        "recovery_verified": {},
        "tool_calls": 0,
        "model_requests": 0,
    }
    for line in response.text.splitlines():
        match = None if line.startswith("#") else SAMPLE.fullmatch(line)
        if match is None or match["name"] not in COUNTED:
            continue
        name = match["name"]
        labels = dict(LABEL.findall(match["labels"] or ""))
        count = int(float(match["value"]))  # every sample read here is a whole number
        if name == "recap_api_requests_total":
            found["api_requests"] += count
            found["api_5xx"] += count if labels.get("status_class") == "5xx" else 0
        elif name == "recap_active_incidents":
            found["active_incidents"] = count
        elif name == "recap_incidents_finished_total" and count:
            found["incidents_finished"][labels["state"]] = count
        elif name == "recap_actions_total" and count:
            found["actions"][labels["outcome"]] = count
        elif name == "recap_recovery_verifications_total" and count:
            found["recovery_verified"][labels["verified"]] = count
        elif name == "recap_tool_calls_total":
            found["tool_calls"] += count
        elif name == "recap_model_requests_total":
            found["model_requests"] += count
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="In-process API load test: concurrent simulated runs, each approved by "
        "its own operator through POST /approvals (see the module docstring)."
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="runs started at once")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, help="the scenario of every run")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds the whole test may take"
    )
    parser.add_argument(
        "--url",
        help="load a running API over HTTP instead (its operator token is read from "
        "RECAP_OPERATOR_TOKEN)",
    )
    parser.add_argument(
        "--loads", default="", help="with --url: the offered loads, one level each, e.g. 4,16,40"
    )
    parser.add_argument(
        "--docker-stats",
        action="store_true",
        help="with --url: sample docker stats while each level runs",
    )
    parser.add_argument("--out", type=Path, help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.url:
        return remote(args, parser)
    os.environ["RECAP_METRICS"] = "1"  # the app counts what it served too (GET /metrics)
    with tempfile.TemporaryDirectory() as tmp:
        # configs/ itself, whatever RECAP_TARGET says: never a live cluster's target profile.
        runtime = Runtime(load_settings(data_dir=Path(tmp), target=""))
        token = secrets.token_urlsafe(32)  # a throwaway operator token for this test only
        app = create_app(runtime, auth=OperatorAuth(token), allowed_hosts=["localhost"])
        try:
            result = asyncio.run(
                load_test(
                    app, runs=args.runs, token=token, scenario=args.scenario, timeout=args.timeout
                )
            )
        except LoadTestRefused as refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 2
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def remote(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """--url: the levels against a running server; the report on stdout (and in --out)."""
    token = os.environ.get("RECAP_OPERATOR_TOKEN", "").strip()
    if not token:
        print("refused: set RECAP_OPERATOR_TOKEN to the server's operator token", file=sys.stderr)
        return 2
    try:
        loads = [int(level) for level in args.loads.split(",") if level.strip()] or [args.runs]
    except ValueError:
        parser.error("--loads takes whole numbers separated by commas, e.g. 4,16,40")
    if min(loads) < 1:
        parser.error("every offered load must be at least 1")
    # The server is built from this repository: its scripted baseline has the same name here.
    provider = scripted_provider(load_settings(target=""))
    try:
        result = asyncio.run(
            load_test_url(
                args.url,
                loads=loads,
                token=token,
                provider=provider,
                scenario=args.scenario,
                timeout=args.timeout,
                docker_stats=args.docker_stats,
            )
        )
    except LoadTestRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=2)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
