"""Optional Prometheus metrics (apps/api/metrics.py): off by default, a well-formed text
format, labels from closed sets only, and counts that follow what runs really did.

A strict reader of the text format below checks every exposition these tests read: each
family is declared (HELP, then TYPE) before its samples, label syntax and escaping are exact,
and histograms are cumulative and end at +Inf with their count.
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from apps.api.main import create_app
from apps.api.metrics import (
    ACTION_OUTCOMES,
    APPROVAL_BUCKETS,
    APPROVAL_DECISIONS,
    FINAL_STATES,
    HTTP_METHODS,
    INCIDENT_BUCKETS,
    JOB_EVENTS,
    LATENCY_BUCKETS,
    MODEL_BUCKETS,
    OTHER,
    PROMETHEUS_TEXT,
    PROVIDER_CLASSES,
    TOOL_OUTCOMES,
    ApiMetrics,
    Counter,
    RequestMetrics,
    render_text,
)
from recap.core.config import Settings
from recap.core.ids import utcnow
from recap.core.models import TraceEvent
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory as C
from recap.workers.queue import WAIT_BUCKETS
from tests.e2e.test_api_contracts import GOLDEN, GOLDEN_SCENARIO
from tests.helpers import OPERATOR_HEADERS, OPERATOR_TOKEN

# --- a strict reader of the text exposition format -----------------------------------------

Series = tuple[str, tuple[tuple[str, str], ...]]  # sample name, its (label, value) pairs sorted

SAMPLE_LINE = re.compile(r"(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{.*\})? (?P<value>\S+)")
LABEL_PAIR = re.compile(r'(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\\n]|\\[\\"n])*)"')
UNESCAPED = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}
# What a run's own ids look like (recap/core/ids.py new_id), and an evidence id.
AN_ID = re.compile(r"\b(?:run|inc|prop|apr|evt|call)-[0-9a-f]{6,}|\bev-\d+-\d+")


def parse(text: str) -> dict[Series, float]:
    """Every sample of an exposition, after checking the format strictly."""
    assert text.endswith("\n") and "\n\n" not in text
    helped: set[str] = set()
    kinds: dict[str, str] = {}
    samples: dict[Series, float] = {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name, _, help_text = line.removeprefix("# HELP ").partition(" ")
            assert name not in helped and help_text, line
            helped.add(name)
        elif line.startswith("# TYPE "):
            name, kind = line.removeprefix("# TYPE ").split(" ")
            assert name in helped and name not in kinds, line  # HELP first, one TYPE each
            assert kind in {"counter", "gauge", "histogram"}, line
            kinds[name] = kind
        else:
            match = SAMPLE_LINE.fullmatch(line)
            assert match, f"not a sample line: {line!r}"
            family = _family(match["name"], kinds)
            assert family is not None, f"a sample of an undeclared family: {line!r}"
            assert kinds[family] != "counter" or family.endswith("_total"), line
            series = (match["name"], tuple(sorted(_labels(match["labels"] or "").items())))
            assert series not in samples, f"the same series twice: {line!r}"
            samples[series] = float(match["value"])
            assert not math.isnan(samples[series]), line
    _check_histograms(samples, kinds)
    return samples


def _family(name: str, kinds: dict[str, str]) -> str | None:
    if name in kinds:
        return name
    for suffix in ("_bucket", "_sum", "_count"):
        base = name.removesuffix(suffix)
        if name.endswith(suffix) and kinds.get(base) == "histogram":
            return base
    return None


def _labels(text: str) -> dict[str, str]:
    if not text:
        return {}
    assert text.startswith("{") and text.endswith("}"), text
    body, labels, position = text[1:-1], {}, 0
    while position < len(body):
        match = LABEL_PAIR.match(body, position)
        assert match, f"bad label syntax at {body[position:]!r}"
        assert match["name"] not in labels, body
        labels[match["name"]] = re.sub(r'\\[\\"n]', lambda m: UNESCAPED[m[0]], match["value"])
        position = match.end()
        if position < len(body):
            assert body[position] == ",", body
            position += 1
    return labels


def _check_histograms(samples: dict[Series, float], kinds: dict[str, str]) -> None:
    for family in (name for name, kind in kinds.items() if kind == "histogram"):
        buckets: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = defaultdict(list)
        for (name, labels), value in samples.items():
            if name == f"{family}_bucket":
                rest = tuple(pair for pair in labels if pair[0] != "le")
                buckets[rest].append((float(dict(labels)["le"]), value))
        for rest, points in buckets.items():
            counts = [count for _, count in sorted(points)]
            assert counts == sorted(counts), f"{family} buckets are not cumulative"
            assert max(bound for bound, _ in points) == math.inf
            assert samples[(f"{family}_count", rest)] == counts[-1]
            assert (f"{family}_sum", rest) in samples


def value(samples: dict[Series, float], name: str, **labels: str) -> float | None:
    """One series' value; None when the exposition does not have it."""
    return samples.get((name, tuple(sorted(labels.items()))))


def total(samples: dict[Series, float], name: str, **labels: str) -> float:
    """The sum of every series of `name` whose labels include `labels`."""
    wanted = set(labels.items())
    return sum(v for (n, pairs), v in samples.items() if n == name and wanted <= set(pairs))


def assert_bounded(text: str, app: FastAPI, settings: Settings) -> None:
    """Every label value comes from its closed set, and nothing id-like or secret is shown."""
    allowed: dict[str, set[str]] = {
        "method": set(HTTP_METHODS),
        "route": {route.path for route in app.routes if isinstance(route, Route)},
        "status_class": {"2xx", "3xx", "4xx", "5xx"},
        "state": set(FINAL_STATES),
        "tool": set(settings.policies.tools),
        "outcome": {*TOOL_OUTCOMES, *ACTION_OUTCOMES},
        "provider_class": set(PROVIDER_CLASSES),
        "direction": {"in", "out"},
        "verified": {"true", "false"},
        "kind": {"simulated", "kind", "sregym"},
        "simulated": {"true", "false"},
        "decision": set(APPROVAL_DECISIONS),
        "alive": {"true", "false"},
        "event": set(JOB_EVENTS),
    }
    bounds = {
        *LATENCY_BUCKETS,
        *INCIDENT_BUCKETS,
        *MODEL_BUCKETS,
        *APPROVAL_BUCKETS,
        *WAIT_BUCKETS,
        math.inf,
    }
    for name, labels in parse(text):
        for label, text_value in labels:
            if label == "le":
                assert float(text_value) in bounds, (name, text_value)
            else:
                assert text_value in allowed[label] | {OTHER}, (name, label, text_value)
    assert AN_ID.search(text) is None
    for secret in (OPERATOR_TOKEN, str(settings.paths.data_dir), str(settings.paths.root)):
        found = secret in text  # a boolean, so a failure never prints the secret
        assert not found
    assert "Bearer" not in text


# --- helpers for runs through the API -------------------------------------------------------


def wait_for(
    predicate: Callable[[], Any],
    timeout: float = 180.0,
    describe: Callable[[], str] | None = None,
) -> Any:
    """The predicate's first truthy answer. A shared CI runner is far slower than a laptop
    (this module once timed out there at 90 s, never locally), so the wait is generous, and
    a timeout says where things stood."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    where = f": {describe()}" if describe is not None else ""
    raise AssertionError(f"condition not met in {timeout:g} s{where}")


def run_with_approval(client: TestClient, scenario: str, **config: Any) -> dict[str, Any]:
    """Start a run, approve its exact proposal as the operator, and return GET /runs/{id}."""
    started = client.post("/incidents/run", json={"scenario_id": scenario, "config": config})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]

    def own_request() -> list[dict[str, Any]]:
        return [r for r in client.get("/approvals/pending").json() if r["run_id"] == run_id]

    [request] = wait_for(own_request)
    decided = client.post(
        f"/approvals/{request['proposal_id']}",
        json={"decision": "approved", "proposal_digest": request["proposal_digest"]},
    )
    assert decided.status_code == 200, decided.text

    def finished() -> dict[str, Any] | None:
        body: dict[str, Any] = client.get(f"/runs/{run_id}").json()
        return body if body["status"] in ("finished", "error") else None  # error: stop at once

    def where() -> str:
        body = client.get(f"/runs/{run_id}").json()
        record = body.get("record") or {}
        return f"status {body['status']}, state {record.get('state')}, error {body.get('error')}"

    body = wait_for(finished, describe=where)
    assert body["status"] == "finished", f"the run ended in error: {body.get('error')}"
    return body  # type: ignore[no-any-return]


def event(
    category: str,
    payload: dict[str, Any],
    *,
    run_id: str = "run-0123456789",
    at: Any = None,
    duration_ms: float | None = None,
) -> TraceEvent:
    return TraceEvent(
        event_id="evt-0123456789",
        run_id=run_id,
        seq=1,
        stage="INVESTIGATING",
        ts=at or utcnow(),
        category=category,
        payload=payload,
        duration_ms=duration_ms,
    )


# --- off by default ---------------------------------------------------------------------------


def test_metrics_are_off_unless_recap_metrics_is_set(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECAP_METRICS", raising=False)
    runtime = Runtime(settings)
    app = create_app(runtime)
    with TestClient(app, headers=OPERATOR_HEADERS) as client:
        absent = client.get("/metrics")
        sinks_while_up = list(runtime.extra_sinks)
    assert absent.status_code == 404
    assert "RECAP_METRICS=1" in absent.json()["detail"]
    assert sinks_while_up == []  # no run is counted
    assert not [m for m in app.user_middleware if m.cls is RequestMetrics]  # no request timed

    monkeypatch.setenv("RECAP_METRICS", "0")
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        assert client.get("/metrics").status_code == 404


def test_recap_metrics_1_turns_them_on_outside_the_typed_api_contract(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_METRICS", "1")
    runtime = Runtime(settings)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        shown = client.get("/metrics")
        sinks_while_up = list(runtime.extra_sinks)
        paths = client.get("/openapi.json").json()["paths"]

    assert shown.status_code == 200
    assert shown.headers["content-type"] == PROMETHEUS_TEXT
    samples = parse(shown.text)
    assert value(samples, "recap_active_incidents") == 0
    for state in FINAL_STATES:  # a closed set of outcomes is shown at 0 from the start
        assert value(samples, "recap_incidents_finished_total", state=state) == 0
    assert len(sinks_while_up) == 1 and runtime.extra_sinks == []  # taken out when it stops
    assert "/metrics" not in paths  # not part of the typed JSON contract (recap-api-v1)


def test_with_the_redis_backend_the_queue_is_scraped_from_redis(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6: depth, capacity, workers and the queue's own job counts, read at each scrape; with
    Redis away, only recap_queue_up 0 (the other metrics are still served)."""
    from recap.workers.queue import JobQueue, QueueConfig
    from tests.fake_redis import FakeRedisServer

    monkeypatch.setenv("RECAP_METRICS", "1")
    with FakeRedisServer.running() as server:
        queue = JobQueue(QueueConfig(url=server.url, capacity=7))
        queue.submit(run_id="run-1", scenario_id="bad-deployment", config={})
        queue.register("worker-a", {"pid": 1})
        with TestClient(
            create_app(Runtime(settings), queue=queue), headers=OPERATOR_HEADERS
        ) as client:
            samples = parse(client.get("/metrics").text)
            assert value(samples, "recap_queue_up") == 1
            assert value(samples, "recap_queue_depth") == 1
            assert value(samples, "recap_queue_capacity") == 7
            assert value(samples, "recap_workers", alive="true") == 1
            assert value(samples, "recap_jobs_total", event="submitted") == 1
            assert value(samples, "recap_jobs_total", event="needs_reconciliation") == 0
            server.stop()
            down = client.get("/metrics")
            assert down.status_code == 200
            samples = parse(down.text)
            assert value(samples, "recap_queue_up") == 0
            assert value(samples, "recap_active_incidents") == 0


# --- the format -------------------------------------------------------------------------------


def test_label_values_and_help_text_are_escaped_and_read_back_exactly() -> None:
    family = Counter("demo_total", "A help text with a backslash \\ and a\nline feed.", ("value",))
    family.inc(value='say "hi"\\now\nplease')
    family.inc(2.5, value="plain")

    text = render_text([family])

    assert "# HELP demo_total A help text with a backslash \\\\ and a\\nline feed.\n" in text
    assert "# TYPE demo_total counter\n" in text
    assert 'demo_total{value="say \\"hi\\"\\\\now\\nplease"} 1\n' in text
    assert 'demo_total{value="plain"} 2.5\n' in text
    samples = parse(text)
    assert value(samples, "demo_total", value='say "hi"\\now\nplease') == 1  # exactly as given
    with pytest.raises(KeyError):
        family.inc(other="label")  # a series always has exactly its family's labels


# --- request metrics --------------------------------------------------------------------------


def test_a_request_is_counted_by_its_route_template_never_its_path(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_METRICS", "1")
    app = create_app(Runtime(settings))
    with TestClient(app, headers=OPERATOR_HEADERS) as client:
        for n in range(30):
            assert client.get(f"/runs/run-abc{n:07d}").status_code == 404
        assert client.get("/runs/run-abc123/timeline", params={"after_seq": 5}).status_code == 404
        client.get("/no/such/page-abc")
        client.request("BREW", "/health")
        refused = client.post("/runs/run-abc123/cancel", headers={"Origin": "http://evil.example"})
        text = client.get("/metrics").text

    samples = parse(text)
    requests = "recap_api_requests_total"
    assert value(samples, requests, method="GET", route="/runs/{run_id}", status_class="4xx") == 30
    timeline = "/runs/{run_id}/timeline"
    assert value(samples, requests, method="GET", route=timeline, status_class="4xx") == 1
    # Refused before routing (a page on another origin), yet counted under its template.
    assert refused.status_code == 403
    cancel = "/runs/{run_id}/cancel"
    assert value(samples, requests, method="POST", route=cancel, status_class="4xx") == 1
    assert total(samples, requests, method=OTHER) == 1  # BREW is not an HTTP method we count
    durations = "recap_api_request_duration_seconds_count"
    assert value(samples, durations, method="GET", route="/runs/{run_id}") == 30
    assert "abc" not in text and "evil" not in text and "page" not in text
    assert_bounded(text, app, settings)


def test_a_request_the_app_fails_on_is_counted_as_the_500_it_gets() -> None:
    metrics = ApiMetrics(enabled=True)
    app = FastAPI()
    app.add_middleware(RequestMetrics, metrics=metrics)

    @app.get("/boom/{item_id}")
    def boom(item_id: str) -> None:
        raise RuntimeError("a bug in an endpoint")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom/item-123").status_code == 500

    samples = parse(metrics.render())
    route = "/boom/{item_id}"
    requests = "recap_api_requests_total"
    assert value(samples, requests, method="GET", route=route, status_class="5xx") == 1
    assert (
        value(samples, "recap_api_request_duration_seconds_count", method="GET", route=route) == 1
    )


# --- what run events say ----------------------------------------------------------------------


def test_run_events_are_counted_into_closed_label_sets(settings: Settings) -> None:
    metrics = ApiMetrics(enabled=True)
    runtime = Runtime(settings)
    created = utcnow()
    with metrics.watching(runtime):
        assert runtime.extra_sinks == [metrics]
        for item in [
            event(C.STATE, {"from": None, "to": "CREATED"}, at=created),
            event(C.TOOL_RESULT, {"tool": "get_service_logs", "ok": True}, duration_ms=12.5),
            # A name a model or a server made up, and one that is not even text.
            event(C.TOOL_RESULT, {"tool": "exfiltrate run-0123456789", "ok": False}),
            event(C.TOOL_RESULT, {"tool": ["not", "a", "name"], "ok": "yes"}),
            # A worker asking for a write is denied: a tool call, not a fix.
            event(
                C.POLICY,
                {"tool": "execute_rollback", "verdict": "DENY", "caller": "telemetry_investigator"},
            ),
            # The controller's fix, denied by the policy: blocked before anything was sent.
            event(
                C.POLICY, {"tool": "delete_namespace", "verdict": "DENY", "caller": "controller"}
            ),
            event(
                C.POLICY,
                {
                    "tool": "execute_rollback",
                    "verdict": "APPROVAL_REQUIRED",
                    "caller": "controller",
                },
            ),
            event(C.VERIFY_EVIDENCE, {"proposal_check": {"ok": False}, "enforced": True}),
            event(C.VERIFY_EVIDENCE, {"proposal_check": {"ok": False}, "enforced": False}),
            event(C.APPROVAL_DECIDED, {"decision": "rejected"}),
            event(C.APPROVAL_DECIDED, {"decision": "expired"}),
            event(C.APPROVAL_DECIDED, {"decision": "refused"}),  # a late answer; another follows
            event(C.APPROVAL_DECIDED, {"decision": "approved"}),
            event(C.MITIGATION, {"tool": "execute_rollback", "ok": True}, duration_ms=300.0),
            event(C.MITIGATION, {"tool": "restart_service", "ok": False}, duration_ms=10_000.0),
            event(C.VERIFY_RECOVERY, {"verified": True}),
            event(C.VERIFY_RECOVERY, {"verified": False}),
            event(C.LLM_CALL, {"origin": "local_llm", "input_tokens": 1200, "output_tokens": 300}),
            event(C.LLM_CALL, {"origin": "replay", "input_tokens": 50, "output_tokens": None}),
            event(C.LLM_CALL, {"origin": "said by a model", "input_tokens": True}),
            event(
                C.STATE, {"from": "VERIFYING", "to": "RESOLVED"}, at=created + timedelta(seconds=42)
            ),
            event(C.STATE, {"from": None, "to": "CREATED"}, run_id="run-fedcba9876", at=created),
        ]:
            metrics.write(item)
        text = metrics.render()
    assert runtime.extra_sinks == []

    samples = parse(text)
    assert value(samples, "recap_active_incidents") == 1  # the second run is still going
    assert value(samples, "recap_incidents_finished_total", state="RESOLVED") == 1
    assert value(samples, "recap_incident_duration_seconds_sum", state="RESOLVED") == 42
    bucket = "recap_incident_duration_seconds_bucket"
    assert value(samples, bucket, state="RESOLVED", le="30") == 0
    assert value(samples, bucket, state="RESOLVED", le="60") == 1

    calls = "recap_tool_calls_total"
    assert value(samples, calls, tool="get_service_logs", outcome="ok") == 1
    assert value(samples, calls, tool=OTHER, outcome="error") == 2
    assert value(samples, calls, tool="execute_rollback", outcome="denied") == 1
    assert value(samples, calls, tool="delete_namespace", outcome="denied") == 1
    assert value(samples, calls, tool="execute_rollback", outcome="ok") == 1  # the write
    assert value(samples, calls, tool="restart_service", outcome="error") == 1
    seconds = "recap_tool_call_duration_seconds"
    assert value(samples, f"{seconds}_sum", tool="get_service_logs") == 0.0125
    assert value(samples, f"{seconds}_bucket", tool="restart_service", le="10") == 1

    actions = {o: value(samples, "recap_actions_total", outcome=o) for o in ACTION_OUTCOMES}
    assert actions == {"executed": 1, "refused": 1, "blocked": 2, "rejected": 1, "expired": 1}
    assert value(samples, "recap_recovery_verifications_total", verified="true") == 1
    assert value(samples, "recap_recovery_verifications_total", verified="false") == 1

    requests = "recap_model_requests_total"
    assert value(samples, requests, provider_class="local_llm") == 1
    assert value(samples, requests, provider_class="replay") == 1
    assert value(samples, requests, provider_class=OTHER) == 1
    tokens = "recap_model_tokens_total"
    assert value(samples, tokens, provider_class="local_llm", direction="in") == 1200
    assert value(samples, tokens, provider_class="local_llm", direction="out") == 300
    assert value(samples, tokens, provider_class="replay", direction="in") == 50
    assert value(samples, tokens, provider_class="replay", direction="out") is None  # unreported
    assert total(samples, tokens, provider_class=OTHER) == 0  # True is not a token count

    assert value(samples, "recap_environment_info", kind="simulated", simulated="true") == 1
    app = create_app(runtime)
    assert_bounded(text, app, settings)


@pytest.mark.parametrize(
    ("kind", "is_simulated", "labels"),
    [
        ("kind", False, {"kind": "kind", "simulated": "false"}),
        ("sregym", False, {"kind": "sregym", "simulated": "false"}),
        ("tests.fakes:FakeEnvironment", True, {"kind": OTHER, "simulated": "true"}),
        (None, None, None),  # the adapter cannot describe itself: the target is left out
    ],
)
def test_the_target_is_named_as_it_is_and_a_live_one_never_as_simulated(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    kind: str | None,
    is_simulated: bool | None,
    labels: dict[str, str] | None,
) -> None:
    monkeypatch.setenv("RECAP_METRICS", "1")
    runtime = Runtime(settings)
    if kind is None:

        def broken() -> Any:
            raise RuntimeError("cannot describe")

        monkeypatch.setattr(runtime, "environment_info", broken)
    else:
        info = runtime.environment_info().model_copy(
            update={"kind": kind, "is_simulated": is_simulated}
        )
        monkeypatch.setattr(runtime, "environment_info", lambda: info)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        shown = client.get("/metrics")

    assert shown.status_code == 200
    targets = [dict(pairs) for name, pairs in parse(shown.text) if name == "recap_environment_info"]
    assert targets == ([labels] if labels else [])


# --- runs through the API ---------------------------------------------------------------------


@pytest.mark.e2e
def test_a_scripted_run_through_the_api_moves_the_incident_tool_and_action_counters(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_METRICS", "1")
    app = create_app(Runtime(settings))
    with TestClient(app, headers=OPERATOR_HEADERS) as client:
        before = parse(client.get("/metrics").text)
        body = run_with_approval(client, "bad-deployment", approval_mode="manual")
        text = client.get("/metrics").text

    record = body["record"]
    assert record["state"] == "RESOLVED"
    after = parse(text)
    assert value(before, "recap_incidents_finished_total", state="RESOLVED") == 0
    assert value(after, "recap_incidents_finished_total", state="RESOLVED") == 1
    assert value(after, "recap_incident_duration_seconds_count", state="RESOLVED") == 1
    assert value(after, "recap_active_incidents") == 0

    calls = "recap_tool_calls_total"
    # Every call the gateway sent, the one write among them: as many as the run's own record.
    sent = total(after, calls, outcome="ok") + total(after, calls, outcome="error")
    assert sent == record["usage"]["tool_calls"] > 1
    assert value(after, calls, tool="execute_rollback", outcome="ok") == 1
    assert total(after, "recap_tool_call_duration_seconds_count") == sent

    actions = {o: value(after, "recap_actions_total", outcome=o) for o in ACTION_OUTCOMES}
    assert actions == {"executed": 1, "refused": 0, "blocked": 0, "rejected": 0, "expired": 0}
    assert value(after, "recap_recovery_verifications_total", verified="true") == 1
    # The scripted baseline is not a model: no model request and no token is claimed.
    assert total(after, "recap_model_requests_total") == 0
    assert total(after, "recap_model_tokens_total") == 0

    requests = "recap_api_requests_total"
    assert value(after, requests, method="POST", route="/incidents/run", status_class="2xx") == 1
    approvals = "/approvals/{proposal_id}"
    assert value(after, requests, method="POST", route=approvals, status_class="2xx") == 1
    assert value(after, "recap_environment_info", kind="simulated", simulated="true") == 1
    assert_bounded(text, app, settings)


@pytest.mark.e2e
def test_a_replayed_model_run_counts_its_recorded_requests_and_tokens_as_replay(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed qwen3:8b two-specialist recording, replayed offline: its requests and the
    tokens the server reported then are counted, under provider_class="replay" only."""
    monkeypatch.setenv("RECAP_METRICS", "1")
    app = create_app(Runtime(settings))
    with TestClient(app, headers=OPERATOR_HEADERS) as client:
        body = run_with_approval(client, GOLDEN_SCENARIO, provider="replay", replay_fixture=GOLDEN)
        usage = client.get(f"/runs/{body['record']['run_id']}/usage").json()
        text = client.get("/metrics").text

    assert body["record"]["state"] == "RESOLVED"
    [model] = usage["models"]
    samples = parse(text)
    requests = "recap_model_requests_total"
    assert value(samples, requests, provider_class="replay") == model["requests"] == 14
    assert total(samples, requests) == model["requests"]  # never counted as live inference
    timed = "recap_model_request_duration_seconds_count"
    assert value(samples, timed, provider_class="replay") == model["requests"]
    tokens = "recap_model_tokens_total"
    assert value(samples, tokens, provider_class="replay", direction="in") == model["input_tokens"]
    assert (
        value(samples, tokens, provider_class="replay", direction="out") == (model["output_tokens"])
    )
    assert value(samples, "recap_incidents_finished_total", state="RESOLVED") == 1
    assert_bounded(text, app, settings)


# --- M6 W6: model and approval time, the queue's wait, occupancy, a worker's own endpoint -----


def test_model_request_time_and_approval_waits_are_timed_with_bounded_labels() -> None:
    """Each model request's time by provider class; each approval's wait from its request to
    its decision, by decision. A decision without a request before it is not timed, and an
    unknown decision counts as other."""
    metrics = ApiMetrics(enabled=True)
    asked = utcnow()
    metrics.write(event(C.LLM_CALL, {"origin": "local_llm"}, duration_ms=2500.0))
    metrics.write(event(C.APPROVAL_REQUESTED, {"proposal_id": "prop-0123456789"}, at=asked))
    later = asked + timedelta(seconds=12)
    metrics.write(event(C.APPROVAL_DECIDED, {"decision": "approved"}, at=later))
    metrics.write(event(C.APPROVAL_DECIDED, {"decision": "approved"}, at=later))  # none asked
    other = "run-abcdef0123"
    metrics.write(event(C.APPROVAL_REQUESTED, {}, run_id=other, at=asked))
    metrics.write(event(C.APPROVAL_DECIDED, {"decision": "maybe"}, run_id=other, at=later))

    samples = parse(metrics.render())
    model = "recap_model_request_duration_seconds"
    assert value(samples, f"{model}_count", provider_class="local_llm") == 1
    assert value(samples, f"{model}_sum", provider_class="local_llm") == 2.5
    assert value(samples, f"{model}_bucket", provider_class="local_llm", le="2.5") == 1
    assert value(samples, f"{model}_bucket", provider_class="local_llm", le="1") == 0
    wait = "recap_approval_wait_seconds"
    assert value(samples, f"{wait}_count", decision="approved") == 1
    assert value(samples, f"{wait}_sum", decision="approved") == 12
    assert value(samples, f"{wait}_count", decision=OTHER) == 1


def test_the_queue_wait_and_worker_occupancy_are_read_from_redis(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new job's wait until its first claim goes into one histogram kept in Redis (every
    worker's claims in one series), and a live worker that holds a job counts as busy."""
    from recap.workers.queue import JobQueue, QueueConfig
    from tests.fake_redis import FakeRedisServer

    monkeypatch.setenv("RECAP_METRICS", "1")
    with FakeRedisServer.running() as server:
        queue = JobQueue(QueueConfig(url=server.url))
        queue.submit(run_id="run-1", scenario_id="bad-deployment", config={})
        queue.submit(run_id="run-2", scenario_id="bad-deployment", config={})
        queue.register("worker-a", {"pid": 1})
        queue.register("worker-b", {"pid": 2})
        assert queue.claim("worker-a", block_seconds=1) is not None
        app = create_app(Runtime(settings), queue=queue)
        with TestClient(app, headers=OPERATOR_HEADERS) as client:
            text = client.get("/metrics").text

    samples = parse(text)
    assert value(samples, "recap_workers", alive="true") == 2
    assert value(samples, "recap_workers_busy") == 1
    assert value(samples, "recap_queue_depth") == 1
    assert value(samples, "recap_queue_wait_seconds_count") == 1
    assert value(samples, "recap_queue_wait_seconds_bucket", le="+Inf") == 1
    waited = value(samples, "recap_queue_wait_seconds_sum")
    assert waited is not None and 0 <= waited < 60
    assert_bounded(text, app, settings)


def test_a_worker_serves_its_own_runs_metrics_and_reads_its_port_strictly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the Redis backend runs execute in workers, so each worker answers GET /metrics with
    its runs' families, without the API's HTTP and queue families; any other path is 404. Its
    port is read strictly, and it listens on loopback unless told otherwise."""
    from urllib.error import HTTPError
    from urllib.request import urlopen

    from apps.worker.metrics import address_from_environment, serve
    from recap.core.errors import ConfigError

    metrics = ApiMetrics(enabled=True)
    metrics.write(event(C.LLM_CALL, {"origin": "local_llm"}, duration_ms=1000.0))
    server = serve(metrics, "127.0.0.1", 0)
    try:
        host, port = server.server_address[:2]
        with urlopen(f"http://{host}:{port}/metrics", timeout=5) as answer:
            assert answer.headers["Content-Type"] == PROMETHEUS_TEXT
            text = answer.read().decode()
        with pytest.raises(HTTPError) as missing:
            urlopen(f"http://{host}:{port}/runs", timeout=5)
        assert missing.value.code == 404
    finally:
        server.shutdown()
    samples = parse(text)
    assert value(samples, "recap_model_requests_total", provider_class="local_llm") == 1
    assert "recap_api_requests_total" not in text and "recap_queue_depth" not in text
    assert_bounded(text, create_app(Runtime(settings)), settings)

    monkeypatch.delenv("RECAP_WORKER_METRICS_PORT", raising=False)
    monkeypatch.delenv("RECAP_WORKER_METRICS_HOST", raising=False)
    assert address_from_environment() is None
    monkeypatch.setenv("RECAP_WORKER_METRICS_PORT", "9100")
    assert address_from_environment() == ("127.0.0.1", 9100)
    monkeypatch.setenv("RECAP_WORKER_METRICS_HOST", "0.0.0.0")
    assert address_from_environment() == ("0.0.0.0", 9100)
    for bad in ("x", "0", "70000"):
        monkeypatch.setenv("RECAP_WORKER_METRICS_PORT", bad)
        with pytest.raises(ConfigError):
            address_from_environment()
