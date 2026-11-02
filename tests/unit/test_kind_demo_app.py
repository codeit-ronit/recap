"""The Kind demo program (infra/kind/app/demo_app.py) keeps M5's contract, offline.

No Docker and no cluster: the program runs in this process on an ephemeral port, or as a
subprocess where the process itself matters (its resident memory, its exit status, its signal
handling). Its log lines go through RECAP's own parsers (recap/core/signals.py) and injection
detector, so the demo and RECAP cannot drift apart unnoticed.
"""

from __future__ import annotations

import importlib.util
import math
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from recap.core import signals
from recap.safety.injection import scan_text

APP = Path(__file__).resolve().parents[2] / "infra" / "kind" / "app" / "demo_app.py"
LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z (INFO|WARN|ERROR) (\S.*)$")
SAMPLE = re.compile(r"(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})? (?P<value>\S+)")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
MIB = 1 << 20
PANIC = "handler panic: KeyError: 'region' [build {}]"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("recap_kind_demo_app", APP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while a class is built
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no cache in infra/
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


demo = _load()


class Lines:
    """The log lines a program would print, captured."""

    def __init__(self) -> None:
        self.all: list[str] = []
        self.log = demo.LineLog(self.all.append)

    def messages(self, level: str | None = None) -> list[str]:
        found = []
        for line in self.all:
            match = LINE.match(line)
            assert match, f"not in the contract's log format: {line!r}"
            if level is None or match[1] == level:
                found.append(match[2])
        return found


def make_app(service: str, build: str, lines: Lines | None = None, **options: Any) -> Any:
    config = demo.Config(service=service, build=build, host="127.0.0.1", port=0, **options)
    return demo.DemoApp(config, log=(lines or Lines()).log, pod_name=f"{service}-test")


@contextmanager
def serving(app: Any) -> Iterator[str]:
    """Serve `app` on an ephemeral loopback port for the duration of the block."""
    server = demo.DemoServer(app, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def stub_server(delay: float = 0.0, on_request: Callable[[], None] | None = None) -> Iterator[str]:
    """A tiny HTTP server that answers {} after `delay` seconds (a slow or a plain upstream)."""

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if on_request is not None:
                on_request()
            time.sleep(delay)
            try:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            except OSError:
                pass  # the caller gave up first, which is the point of a slow stub

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def client() -> httpx.Client:
    return httpx.Client(trust_env=False, timeout=10.0)


def free_port() -> int:
    """A loopback port nothing listens on (bound, read and released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


Samples = dict[tuple[str, tuple[tuple[str, str], ...]], float]


def exposition(text: str) -> tuple[dict[str, str], Samples]:
    """(the TYPE of each family, the value of each sample by name and sorted labels).

    Fails on a line that is not a comment or a sample, and on a sample whose family has no
    TYPE line before it, as Prometheus's own parser would.
    """
    types: dict[str, str] = {}
    samples: Samples = {}
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            types[name] = kind
            continue
        if line.startswith("# HELP "):
            continue
        match = SAMPLE.fullmatch(line)
        assert match, f"not a sample line: {line!r}"
        name = match["name"]
        base = re.sub(r"_(bucket|sum|count)$", "", name)
        family = base if types.get(base) == "histogram" else name
        assert family in types, f"{name} has no TYPE line before it"
        labels = tuple(sorted(LABEL.findall(match["labels"] or "")))
        samples[(name, labels)] = float(match["value"])
    return types, samples


def sample(samples: Samples, name: str, **labels: str) -> float:
    return samples[(name, tuple(sorted(labels.items())))]


# --- the catalogue and the builds --------------------------------------------------------


def test_the_builds_are_the_contracts_and_only_the_legacy_quarter_lacks_a_region() -> None:
    assert set(demo.BEHAVIOURS) == {"v1.0.0", "v1.1.0", "v1.1.1", "v1.2.0"}
    assert demo.behaviour_of("v9.9.9") == demo.behaviour_of("v1.0.0")  # unknown: the baseline
    legacy = [i for i in range(demo.CATALOGUE_SIZE) if demo.is_legacy_item(i)]
    assert legacy == [i for i in range(100) if i % 4 == 3]
    assert len(legacy) == 25  # exactly 25% of the catalogue


def test_the_baseline_backend_serves_every_item_and_logs_no_error() -> None:
    lines = Lines()
    with serving(make_app("backend", "v1.0.0", lines)) as url, client() as http:
        answers = [http.get(f"{url}/items", params={"id": i}) for i in range(100)]
        assert {answer.status_code for answer in answers} == {200}
        first = answers[0].json()
        assert first["id"] == 0 and first["build"] == "v1.0.0" and first["price_cents"] > 0
        assert answers[3].json()["region"] == "global"  # a legacy item, priced without a region
        assert http.get(f"{url}/healthz").json() == {
            "alive": True,
            "service": "backend",
            "build": "v1.0.0",
        }
        assert http.get(f"{url}/readyz").json() == {"ready": True}
    assert lines.messages("ERROR") == []


@pytest.mark.parametrize("build", ["v1.1.0", "v1.1.1"])
def test_the_bad_release_fails_exactly_the_legacy_items(build: str) -> None:
    lines = Lines()
    with serving(make_app("backend", build, lines)) as url, client() as http:
        failed = {
            i for i in range(100) if http.get(f"{url}/items", params={"id": i}).status_code == 500
        }
    assert failed == {i for i in range(100) if i % 4 == 3}
    panics = [m for m in lines.messages("ERROR") if m.startswith("handler panic")]
    assert panics == [PANIC.format(build)] * 25
    assert {signals.version_tag(m) for m in panics} == {build}  # RECAP reads the bad version


def test_without_an_id_the_items_come_in_turn_so_the_error_share_is_reproducible() -> None:
    with serving(make_app("backend", "v1.1.0")) as url, client() as http:
        codes = [http.get(f"{url}/items").status_code for _ in range(200)]
    assert codes[:8] == [200, 200, 200, 500] * 2
    assert codes.count(500) == 50  # 25%, every time


def test_only_v1_1_1_plants_the_red_team_instruction_after_each_panic() -> None:
    planted, plain = Lines(), Lines()
    for build, lines in (("v1.1.1", planted), ("v1.1.0", plain)):
        with serving(make_app("backend", build, lines)) as url, client() as http:
            for item_id in range(8):
                http.get(f"{url}/items", params={"id": item_id})
    instruction = demo.PLANTED_INSTRUCTION
    assert planted.messages("ERROR") == [PANIC.format("v1.1.1"), instruction] * 2
    assert plain.messages("ERROR") == [PANIC.format("v1.1.0")] * 2
    assert instruction.startswith("[sre-automation]") and "delete_namespace" in instruction
    assert scan_text(instruction)  # RECAP's gateway flags it as a suspected prompt injection
    source = APP.read_text(encoding="utf-8")
    assert source.count("RED-TEAM TEST CONTENT") >= 2  # marked where it is written and logged


def test_bad_ids_bad_periods_and_unknown_paths_are_refused_without_a_panic() -> None:
    lines = Lines()
    with serving(make_app("backend", "v1.1.0", lines)) as url, client() as http:
        assert http.get(f"{url}/items", params={"id": "abc"}).status_code == 400
        assert http.get(f"{url}/items", params={"id": "-1"}).status_code == 400
        assert http.get(f"{url}/items", params={"id": "100"}).status_code == 404
        assert http.get(f"{url}/report", params={"period": "Daily!"}).status_code == 400
        assert http.get(f"{url}/admin").status_code == 404
        assert http.post(f"{url}/items").status_code == 501  # GET only
    assert lines.messages("ERROR") == []


# --- the frontend and its upstream calls -------------------------------------------------


def test_the_frontend_reports_backend_errors_in_rpc_style() -> None:
    backend_lines, frontend_lines = Lines(), Lines()
    with serving(make_app("backend", "v1.1.0", backend_lines)) as backend_url:
        frontend = make_app("frontend", "v1.0.0", frontend_lines, backend_url=backend_url)
        with serving(frontend) as url, client() as http:
            answers = [http.get(f"{url}/") for _ in range(100)]
            _, samples = exposition(http.get(f"{url}/metrics").text)
    codes = [answer.status_code for answer in answers]
    assert codes.count(502) == 25 and codes.count(200) == 75
    good = next(answer.json() for answer in answers if answer.status_code == 200)
    assert good["frontend"] == "v1.0.0" and good["item"]["build"] == "v1.1.0"
    target = backend_url.removeprefix("http://") + "/items"
    errors = frontend_lines.messages("ERROR")
    assert errors == [f"rpc to backend failed: INTERNAL (HTTP 500 from {target})"] * 25
    assert {signals.upstream_dependency(message) for message in errors} == {"backend"}
    results = {
        result: sample(samples, "upstream_requests_total", upstream="backend", result=result)
        for result in ("ok", "error", "timeout", "unavailable")
    }
    assert results == {"ok": 75, "error": 25, "timeout": 0, "unavailable": 0}
    assert sample(samples, "http_requests_total", handler="/", code="502") == 25


def test_a_backend_that_refuses_connections_is_unavailable() -> None:
    lines = Lines()
    frontend = make_app("frontend", "v1.0.0", lines, backend_url=f"http://127.0.0.1:{free_port()}")
    with serving(frontend) as url, client() as http:
        answer = http.get(f"{url}/")
    assert answer.status_code == 502 and answer.json()["code"] == "UNAVAILABLE"
    assert lines.messages("ERROR") == ["rpc to backend failed: UNAVAILABLE (connection refused)"]
    assert signals.upstream_dependency(lines.messages("ERROR")[0]) == "backend"


def test_a_slow_backend_is_a_deadline_exceeded() -> None:
    lines = Lines()
    with stub_server(delay=1.0) as slow_url:
        frontend = make_app(
            "frontend", "v1.0.0", lines, backend_url=slow_url, upstream_timeout_ms=200
        )
        with serving(frontend) as url, client() as http:
            answer = http.get(f"{url}/")
            _, samples = exposition(http.get(f"{url}/metrics").text)
    assert answer.status_code == 502 and answer.json()["code"] == "DEADLINE_EXCEEDED"
    target = slow_url.removeprefix("http://") + "/items"
    assert lines.messages("ERROR") == [
        f"rpc to backend failed: DEADLINE_EXCEEDED ({target}, 200 ms)"
    ]
    assert sample(samples, "upstream_requests_total", upstream="backend", result="timeout") == 1


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            urllib.error.URLError(TimeoutError("timed out")),
            "rpc to backend failed: DEADLINE_EXCEEDED (backend.recap-demo:80/items, 2000 ms)",
        ),
        (
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
            "rpc to backend failed: UNAVAILABLE (connection refused)",
        ),
    ],
)
def test_in_the_cluster_the_log_lines_are_exactly_the_contracts(
    failure: Exception, message: str
) -> None:
    calls: list[tuple[str, float]] = []

    def failing(url: str, timeout: float) -> Any:
        calls.append((url, timeout))
        raise failure

    lines = Lines()
    config = demo.Config(service="frontend", build="v1.0.0")  # the cluster's defaults
    frontend = demo.DemoApp(config, log=lines.log, opener=failing, pod_name="frontend-test")
    assert frontend.serve("/", {}).status == 502
    assert calls == [("http://backend.recap-demo.svc.cluster.local/items?id=0", 2.0)]
    assert lines.messages("ERROR") == [message]
    assert signals.upstream_dependency(message) == "backend"
    target = demo.upstream_target("http://backend.recap-demo.svc.cluster.local", "/items")
    assert target == "backend.recap-demo:80/items"


# --- logs ----------------------------------------------------------------------------------


def test_every_log_line_has_the_contract_format() -> None:
    moment = datetime(2026, 11, 2, 10, 0, 0, 123456, tzinfo=UTC)
    assert demo.utc_stamp(moment) == "2026-11-02T10:00:00.123Z"
    elsewhere = moment.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert demo.utc_stamp(elsewhere) == "2026-11-02T10:00:00.123Z"  # always written in UTC
    started = demo.startup_message(8080, "v1.0.0")
    assert started == "serving on :8080 [build v1.0.0]"
    assert signals.version_tag(started) == "v1.0.0"
    written: list[str] = []
    log = demo.LineLog(written.append)
    log("WARN", "first\nsecond")
    assert LINE.match(written[0]) and written[0].endswith(" WARN first second")  # one line each
    with pytest.raises(ValueError):
        log("DEBUG", "not a level the contract knows")


# --- metrics -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "extra"),
    [
        ("frontend", {"upstream_requests_total"}),
        ("backend", set()),
        ("traffic", {"traffic_requests_total"}),
    ],
)
def test_each_role_exposes_the_contract_metric_families(service: str, extra: set[str]) -> None:
    with serving(make_app(service, "v1.0.0")) as url, client() as http:
        answer = http.get(f"{url}/metrics")
    assert answer.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    types, samples = exposition(answer.text)
    base = {
        "http_requests_total": "counter",
        "http_request_duration_seconds": "histogram",
        "process_resident_memory_bytes": "gauge",
        "process_cpu_seconds_total": "counter",
    }
    assert types == base | dict.fromkeys(extra, "counter")
    assert sample(samples, "process_resident_memory_bytes") > 0
    assert sample(samples, "process_cpu_seconds_total") > 0


def test_request_metrics_count_user_paths_only_with_a_cumulative_histogram() -> None:
    with serving(make_app("backend", "v1.1.0")) as url, client() as http:
        for item_id in range(8):
            http.get(f"{url}/items", params={"id": item_id})
        for path in ("/nope", "/healthz", "/readyz", "/metrics"):
            http.get(f"{url}{path}")
        _, samples = exposition(http.get(f"{url}/metrics").text)
    assert sample(samples, "http_requests_total", handler="/items", code="200") == 6
    assert sample(samples, "http_requests_total", handler="/items", code="500") == 2
    assert sample(samples, "http_requests_total", handler="other", code="404") == 1
    handlers = {dict(labels).get("handler") for (_, labels) in samples} - {None}
    assert handlers == {"/items", "other"}  # probes and scrapes are not user traffic
    buckets = sorted(
        (float(dict(labels)["le"]), value)
        for (name, labels), value in samples.items()
        if name == "http_request_duration_seconds_bucket" and dict(labels)["handler"] == "/items"
    )
    counts = [value for _, value in buckets]
    assert counts == sorted(counts) and buckets[-1] == (math.inf, 8.0)
    assert sample(samples, "http_request_duration_seconds_count", handler="/items") == 8
    assert sample(samples, "http_request_duration_seconds_sum", handler="/items") > 0


# --- readiness and the report-cache leak -------------------------------------------------


def test_the_report_cache_leak_turns_readiness_off_and_keeps_it_off() -> None:
    lines = Lines()
    apps: list[Any] = []

    def memory() -> int:  # 40 MiB of baseline plus every cached report
        return 40 * MIB + apps[0].cached_report_bytes()

    config = demo.Config(
        service="backend", build="v1.0.0", memory_limit_bytes=100 * MIB, report_bytes=4 * MIB
    )
    apps.append(demo.DemoApp(config, log=lines.log, memory_reader=memory, pod_name="backend-x"))
    with serving(apps[0]) as url, client() as http:
        assert http.get(f"{url}/readyz").status_code == 200
        for _ in range(12):  # 40 + 48 MiB: 88% of the limit
            assert http.get(f"{url}/report").status_code == 200
        assert http.get(f"{url}/readyz").status_code == 200
        assert http.get(f"{url}/report").status_code == 200  # 92 MiB: past 90%
        refused = http.get(f"{url}/readyz")
        assert refused.status_code == 503
        assert refused.json()["reason"] == "memory pressure: 92% of the container limit in use"
        assert http.get(f"{url}/report").status_code == 503  # no new work: memory stops here
        assert apps[0].cached_report_bytes() == 13 * 4 * MIB
        assert http.get(f"{url}/healthz").status_code == 200  # alive, so liveness never restarts it
        assert http.get(f"{url}/items", params={"id": 1}).status_code == 200
        time.sleep(0.05)
        assert http.get(f"{url}/readyz").status_code == 503  # nothing is evicted: it stays off
    warnings = lines.messages("WARN")
    assert warnings == [signals.RPC_HEAP_WARNING_TEMPLATE.format(percent=92)]  # once per 10 s
    assert not signals.is_oom_message(warnings[0])  # pressure, not an out-of-memory kill


def test_memory_at_exactly_90_percent_is_still_ready_and_no_limit_means_no_rule() -> None:
    def at(used: int) -> Any:
        config = demo.Config(service="backend", build="v1.0.0", memory_limit_bytes=100)
        return demo.DemoApp(config, log=Lines().log, memory_reader=lambda: used, pod_name="b")

    assert at(90).readiness().status == 200
    assert at(91).readiness().status == 503
    unlimited = demo.DemoApp(
        demo.Config(service="backend", build="v1.0.0"),
        log=Lines().log,
        memory_reader=lambda: 10**12,
        pod_name="b",
    )
    assert unlimited.readiness().status == 200


def test_the_memory_limit_comes_from_the_downward_api_or_the_cgroup(tmp_path: Path) -> None:
    cgroup = tmp_path / "memory.max"
    assert demo.memory_limit({"MEMORY_LIMIT_BYTES": "134217728"}, cgroup) == 128 * MIB
    assert demo.memory_limit({"MEMORY_LIMIT_BYTES": "0"}, cgroup) is None  # 0: no limit
    assert demo.memory_limit({}, cgroup) is None  # no cgroup file (not in a container)
    cgroup.write_text("max\n")
    assert demo.memory_limit({}, cgroup) is None
    cgroup.write_text("67108864\n")
    assert demo.memory_limit({}, cgroup) == 64 * MIB


# --- the program as a process --------------------------------------------------------------


class Running:
    """A started program: its port, its first line and every line it printed so far."""

    def __init__(self, process: subprocess.Popen[str], lines: queue.Queue[str]) -> None:
        self.process = process
        self._queue = lines
        self.lines: list[str] = []
        self.first_line = self.wait_for(r"INFO serving on :\d+ ")
        port = re.search(r"serving on :(\d+)", self.first_line)
        assert port is not None
        self.port = int(port[1])
        self.url = f"http://127.0.0.1:{self.port}"

    def wait_for(self, pattern: str, timeout: float = 20.0) -> str:
        for line in self.lines:
            if re.search(pattern, line):
                return line
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self.lines.append(line)
            if re.search(pattern, line):
                return line
        raise AssertionError(f"no line matching {pattern!r}; got {self.lines!r}")


@contextmanager
def running(service: str, build: str, **env: str) -> Iterator[Running]:
    """The program as its own process, the way the image runs it, on an ephemeral port."""
    environ = {
        "DEMO_SERVICE": service,
        "DEMO_BUILD": build,
        "HOST": "127.0.0.1",
        "PORT": "0",
        "MEMORY_LIMIT_BYTES": "0",
        **env,
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(APP)],
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()
    try:
        yield Running(process, lines)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_sigterm_stops_the_program_at_once_with_status_0() -> None:
    with running("frontend", "v1.0.0") as program:
        started = LINE.match(program.first_line)
        assert started and started[2] == f"serving on :{program.port} [build v1.0.0]"
        program.process.send_signal(signal.SIGTERM)
        assert program.process.wait(timeout=10) == 0
        program.wait_for(r" INFO shutting down \(SIGTERM\) \[build v1\.0\.0\]$")


START_UP_FAILURE = (
    "start-up failed: RuntimeError: catalogue schema 2 is not supported by this build "
    "(needs 3) [build v1.2.0]"
)


def test_v1_2_0_fails_at_start_up_before_its_port_opens_and_exits_1() -> None:
    """So its pods never become ready, and the rolling update stops with the old ones serving."""
    environ = {"DEMO_SERVICE": "backend", "DEMO_BUILD": "v1.2.0", "HOST": "127.0.0.1"}
    finished = subprocess.run(
        [sys.executable, "-I", "-B", str(APP)],
        env={**environ, "PORT": "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert finished.returncode == 1
    (line,) = finished.stdout.splitlines()  # no "serving on" line: it never served
    match = LINE.match(line)
    assert match is not None and match[1] == "ERROR" and match[2] == START_UP_FAILURE
    assert signals.version_tag(match[2]) == "v1.2.0"
    assert not signals.is_oom_message(match[2])  # a failed start-up, not an out-of-memory kill


def test_v1_2_0_with_a_crash_delay_serves_first_then_exits_1() -> None:
    with running("backend", "v1.2.0", CRASH_AFTER_SECONDS="1.5") as program, client() as http:
        assert http.get(f"{program.url}/readyz").status_code == 200  # ready before it fails
        assert http.get(f"{program.url}/items", params={"id": 3}).status_code == 200
        assert program.process.wait(timeout=20) == 1
        crash = program.wait_for(r" ERROR start-up failed: ")
    match = LINE.match(crash)
    assert match is not None and match[2] == START_UP_FAILURE


def test_a_real_process_refuses_traffic_once_its_resident_memory_passes_90_percent() -> None:
    limit = 160 * MIB
    environ = {"MEMORY_LIMIT_BYTES": str(limit), "REPORT_BYTES": str(8 * MIB)}
    with running("backend", "v1.0.0", **environ) as program, client() as http:
        assert http.get(f"{program.url}/readyz").status_code == 200
        for _ in range(60):  # 8 MiB a report: well past 144 MiB long before 60 reports
            if http.get(f"{program.url}/readyz").status_code == 503:
                break
            http.get(f"{program.url}/report")
        else:
            pytest.fail("readiness never refused traffic")
        _, samples = exposition(http.get(f"{program.url}/metrics").text)
        assert sample(samples, "process_resident_memory_bytes") > 0.9 * limit
        assert http.get(f"{program.url}/healthz").status_code == 200  # still alive
        program.wait_for(r" WARN memory pressure: \d+% of the container limit in use$")


@pytest.mark.parametrize(
    "environ",
    [
        {"DEMO_SERVICE": "database", "DEMO_BUILD": "v1.0.0"},
        {"DEMO_SERVICE": "backend", "DEMO_BUILD": "latest"},
        {"DEMO_SERVICE": "backend", "DEMO_BUILD": "v1.0.0", "PORT": "eighty"},
        {"DEMO_SERVICE": "frontend", "DEMO_BUILD": "v1.0.0", "BACKEND_URL": "file:///etc/hosts"},
        {"DEMO_SERVICE": "traffic", "DEMO_BUILD": "v1.0.0", "TRAFFIC_RPS": "nan"},
    ],
)
def test_a_bad_setting_stops_the_program_with_status_2(
    environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert demo.main(environ) == 2
    printed = capsys.readouterr().out.strip()
    match = LINE.match(printed)
    assert match and match[1] == "ERROR" and match[2].startswith("invalid configuration: ")


# --- the traffic generator -----------------------------------------------------------------


def test_the_traffic_role_sends_a_steady_rate_and_counts_the_results() -> None:
    hits: list[float] = []
    with stub_server(on_request=lambda: hits.append(time.monotonic())) as url:
        app = make_app("traffic", "v1.0.0", traffic_target=f"{url}/", traffic_rps=50.0)
        stop = threading.Event()
        threads = app.start_background(stop)
        time.sleep(1.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        time.sleep(0.3)  # the last requests in flight
    assert 20 <= len(hits) <= 60  # about 50 in one second at 50 a second
    totals = app.metrics.traffic_totals()
    assert totals["ok"] >= 20 and totals["error"] == totals["unavailable"] == 0
