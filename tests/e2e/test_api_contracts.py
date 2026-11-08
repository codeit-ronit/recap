"""The typed API contracts (apps/api/dto.py, schema "recap-api-v1") over real runs.

Each test drives incidents through the HTTP API (controller, MCP subprocess, gateway,
simulator, approvals, oracle) and reads them back through every typed view: a run that is
still running, one that resolved, failed, was cancelled, was rejected by the operator, ran
out of budget, was blocked by the safety policy, was REPLAYED from the committed golden
recording of a real qwen3:8b run (offline: no model is called), and runs that do not exist
or whose trace is missing. Every response is validated against its published model, so a
field the server forgets or invents fails here.

No test calls a model or the local Ollama server: model runs use the fake server of
tests/llm_fakes.py, replays answer from fixtures/recordings/.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.dto import (
    API_SCHEMA,
    SCHEMA_HEADER,
    TOTAL_HEADER,
    BatchView,
    ComponentsView,
    EvalsView,
    EvidenceView,
    ProviderView,
    ReplaysView,
    RetrievalView,
    RunDetail,
    RunStarted,
    RunSummary,
    SystemView,
    TimelineView,
    UsageView,
)
from apps.api.main import create_app
from apps.api.service import RunRefused, check_run_request
from apps.api.views import CONCURRENCY_NOTE, REPLAY_NOTE, SCRIPTED_NOTE
from evals.components import ComponentReport, write_report
from evals.records import Comparison, EvalBatch, EvalConfiguration, EvalRecord, write_batch
from recap.core.config import Settings
from recap.core.models import EnvironmentInfo, IncidentState, ProviderInfo, RunConfig, RunRecord
from recap.providers.factory import REPLAY_ENV, create_provider
from recap.providers.recordings import MANIFEST, list_fixtures
from recap.runtime import Runtime
from tests.helpers import OPERATOR_HEADERS, OPERATOR_TOKEN
from tests.llm_fakes import FAKE_GROQ_KEY, FakeOllama, ScriptedModel

GOLDEN = "qwen3-8b-unfamiliar-parallel"  # the owner-approved two-specialist run (M3)
GOLDEN_SCENARIO = "m3-inventory-leak-behind-frontend"
RUN_VIEWS: dict[str, Any] = {
    "": RunDetail,
    "/timeline": TimelineView,
    "/evidence": EvidenceView,
    "/retrieval": RetrievalView,
    "/usage": UsageView,
}


# --- helpers ----------------------------------------------------------------------------------


@pytest.fixture
def no_ollama_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /providers asks a local Ollama server for its models: answer with the fake one."""
    import apps.api.main as api_main

    real = api_main.provider_status

    async def with_fake_ollama(settings_: Settings, ledger: Any, **kwargs: Any) -> list:
        return await real(settings_, ledger, mock_http=FakeOllama().mock())

    monkeypatch.setattr(api_main, "provider_status", with_fake_ollama)


@pytest.fixture(autouse=True)
def no_replay_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay here is chosen through the API, never by a developer's shell variable."""
    monkeypatch.delenv(REPLAY_ENV, raising=False)


def client_for(runtime: Runtime) -> TestClient:
    return TestClient(create_app(runtime), headers=OPERATOR_HEADERS)


def wait_for(predicate: Callable[[], Any], timeout: float = 90.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def start(client: TestClient, scenario: str = "bad-deployment", **config: Any) -> str:
    response = client.post("/incidents/run", json={"scenario_id": scenario, "config": config})
    assert response.status_code == 202, response.text
    RunStarted.model_validate(response.json())
    return response.json()["run_id"]


def finished(client: TestClient, run_id: str) -> dict[str, Any]:
    def done() -> dict[str, Any] | None:
        body = client.get(f"/runs/{run_id}").json()
        return body if body["status"] != "running" else None

    return wait_for(done)


def pending_for(client: TestClient, run_id: str) -> dict[str, Any]:
    def found() -> dict[str, Any] | None:
        pending = client.get("/approvals/pending").json()
        return next((p for p in pending if p["run_id"] == run_id), None)

    return wait_for(found)


def decide(client: TestClient, pending: dict[str, Any], decision: str) -> None:
    body = {"decision": decision, "proposal_digest": pending["proposal_digest"]}
    response = client.post(f"/approvals/{pending['proposal_id']}", json=body)
    assert response.status_code == 200, response.text


def views(client: TestClient, run_id: str) -> dict[str, dict[str, Any]]:
    """Every typed view of one run, each checked against its published contract."""
    found = {}
    for suffix, model in RUN_VIEWS.items():
        response = client.get(f"/runs/{run_id}{suffix}")
        assert response.status_code == 200, (suffix, response.text)
        assert response.headers[SCHEMA_HEADER] == API_SCHEMA
        body = response.json()
        assert body["schema_version"] == API_SCHEMA
        model.model_validate(body)  # exactly the published shape: nothing missing, nothing extra
        found[suffix or "/detail"] = body
    return found


def entries(view: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [e for e in view["entries"] if e["kind"] == kind]


def whole_timeline(client: TestClient, run_id: str, limit: int = 25) -> list[dict[str, Any]]:
    """Every timeline entry, page by page with the cursor the API returns."""
    found: list[dict[str, Any]] = []
    after = 0
    while True:
        page = client.get(f"/runs/{run_id}/timeline", params={"after_seq": after, "limit": limit})
        body = page.json()
        found += body["entries"]
        after = body["next_after_seq"]
        if not body["has_more"]:
            return found


def stream_ids(text: str) -> list[int]:
    return [int(line[4:]) for line in text.splitlines() if line.startswith("id: ")]


# --- completed, live, cancelled, rejected -----------------------------------------------------


@pytest.mark.e2e
def test_a_resolved_run_reads_the_same_through_every_view(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        run_id = start(client, approval_mode="manual")
        pending = pending_for(client, run_id)
        decide(client, pending, "approved")
        finished(client, run_id)
        run = views(client, run_id)
        listed = client.get("/runs").json()

    summary = run["/detail"]["summary"]
    assert (summary["state"], summary["outcome"], summary["terminal"]) == (
        "RESOLVED",
        "resolved",
        True,
    )
    assert summary["oracle"]["verified"] and summary["oracle"]["healthy_after"]
    assert summary["labels"] == ["SIMULATED ENVIRONMENT", "SCRIPTED BASELINE (NOT AN LLM)"]
    assert (summary["provider_class"], summary["model"], summary["prompt_version"]) == (
        "scripted",
        None,
        None,
    )
    assert summary["ended_at"] >= summary["started_at"] and summary["not_recorded"] == []
    assert listed[0] == summary  # GET /runs serves the same summary
    # The stored record and evidence keep the shape the dashboard has always read.
    assert run["/detail"]["record"]["state"] == "RESOLVED" and run["/detail"]["evidence"]

    timeline = run["/timeline"]
    [decided] = entries(timeline, "approval_decided")
    assert decided["verdict"] == "approved"
    assert decided["payload"]["actor"] == "operator:local-operator"
    [write] = entries(timeline, "write_executed")
    assert write["ok"] is True and write["evidence_ids"]
    assert [(lane["agent"], lane["round"]) for lane in timeline["lanes"]] == [("investigator", 1)]

    evidence = run["/evidence"]
    assert {item["trust_class"] for item in evidence["registry"]} == {"untrusted_tool_output"}
    [proposal] = evidence["proposals"]
    assert proposal["digest"] == pending["proposal_digest"]  # what the operator approved
    assert proposal["author"] == "scripted_policy"
    assert [v["verdict"] for v in evidence["policy_verdicts"]] == ["APPROVAL_REQUIRED"]
    best = evidence["final_report"]["best"]
    assert (best["mechanism"], best["target_service"]) == ("bad_deployment", "checkout")

    usage = run["/usage"]
    assert usage["models"] == [] and usage["notes"] == [SCRIPTED_NOTE]
    assert usage["cost_label"].startswith("$0.00")
    assert usage["estimated_context_tokens"]["estimate"] is True
    assert run["/retrieval"]["searches"][0]["query_author"] == "recap"


@pytest.mark.e2e
def test_a_live_run_reads_as_in_progress_and_then_as_cancelled(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        run_id = start(client, approval_mode="manual")
        pending_for(client, run_id)  # it waits for the operator now
        live = views(client, run_id)
        assert client.post(f"/runs/{run_id}/cancel").status_code == 202
        finished(client, run_id)
        cancelled = views(client, run_id)

    summary = live["/detail"]["summary"]
    assert (summary["status"], summary["state"], summary["outcome"]) == (
        "running",
        "AWAITING_APPROVAL",
        "in_progress",
    )
    assert not summary["terminal"] and summary["pending_approval"]
    assert summary["ended_at"] is None and summary["oracle"] is None
    assert live["/timeline"]["live"] is True
    assert live["/usage"]["budget"]["used"] is None  # no run summary yet: not recorded
    assert "run summary (budget used, cost label)" in live["/usage"]["not_recorded"]

    summary = cancelled["/detail"]["summary"]
    assert (summary["state"], summary["outcome"]) == ("FAILED", "cancelled")
    assert summary["outcome_note"] == "run cancelled" and summary["ended_at"] is not None
    timeline = cancelled["/timeline"]
    # Cancelled while it waited: no decision was ever made, and nothing was written.
    assert entries(timeline, "approval_decided") == [] and timeline["retries"]["fix_attempts"] == 0
    assert [e["payload"]["to"] for e in entries(timeline, "lifecycle")][-1] == "FAILED"
    assert not timeline["live"]


@pytest.mark.e2e
def test_a_fix_the_operator_rejects_reads_as_approval_rejected(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        run_id = start(client, approval_mode="manual")
        decide(client, pending_for(client, run_id), "rejected")
        finished(client, run_id)
        run = views(client, run_id)

    summary = run["/detail"]["summary"]
    assert (summary["state"], summary["outcome"]) == ("BLOCKED", "approval_rejected")
    assert summary["oracle"] is None  # nothing was executed, so nothing to verify
    timeline = run["/timeline"]
    assert [e["verdict"] for e in entries(timeline, "approval_decided")] == ["rejected"]
    assert "write_executed" not in timeline["counts"] and timeline["retries"]["fix_attempts"] == 0


@pytest.mark.e2e
def test_a_run_out_of_budget_reads_as_budget_exhausted(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        run_id = start(client, "budget-exhausted", approval_mode="auto")
        finished(client, run_id)
        run = views(client, run_id)

    assert run["/detail"]["summary"]["outcome"] == "budget_exhausted"
    [stop] = entries(run["/timeline"], "budget")
    assert stop["payload"]["limit"] == "max_tool_calls"
    used = run["/usage"]["budget"]["used"]
    assert used["tool_calls"] == used["max_tool_calls"] == 3


@pytest.mark.e2e
def test_an_unsafe_fix_attempted_is_blocked_and_never_executed(settings: Settings) -> None:
    """The red-team planner of `malicious-log` proposes deleting the namespace."""
    with client_for(Runtime(settings)) as client:
        run_id = start(client, "malicious-log", approval_mode="auto")
        finished(client, run_id)
        run = views(client, run_id)

    assert run["/detail"]["summary"]["outcome"] == "safety_blocked"
    evidence = run["/evidence"]
    denial = next(v for v in evidence["policy_verdicts"] if v["tool"] == "delete_namespace")
    assert denial["verdict"] == "DENY" and "R9-destructive-deny" in denial["rule_ids"]
    assert evidence["injection_flags"], "the planted log line is flagged"
    timeline = run["/timeline"]
    assert timeline["retries"]["fix_attempts"] == 0  # attempted, never executed
    [summary_event] = entries(timeline, "summary")
    assert summary_event["payload"]["unsafe_attempts"] >= 1
    assert summary_event["payload"]["unsafe_executed"] == 0


@pytest.mark.e2e
def test_a_run_whose_model_server_fails_reads_as_an_error_with_its_requests(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    broken = FakeOllama(responder=lambda body: httpx.Response(500, json={"error": "out of memory"}))

    def make(config: RunConfig) -> Any:
        return create_provider(
            "ollama", runtime.settings, mock_http=broken.mock(), ledger=runtime.ledger
        )

    runtime.make_provider = make  # type: ignore[method-assign]
    with client_for(runtime) as client:
        run_id = start(client, approval_mode="auto", use_memory=False)
        finished(client, run_id)
        run = views(client, run_id)

    summary = run["/detail"]["summary"]
    assert (summary["state"], summary["outcome"]) == ("FAILED", "error")
    assert "HTTP 500" in summary["error"]
    assert (summary["provider_class"], summary["model"]) == ("local_llm", "qwen3:8b")
    [model] = run["/usage"]["models"]
    assert model["failed_requests"] == model["requests"] >= 1
    assert model["input_tokens"] is None  # the server reported none: never an estimate here
    assert model["requests_without_token_counts"] == model["requests"]
    assert run["/timeline"]["retries"]["failed_model_requests"] == model["requests"]


# --- the golden replay ------------------------------------------------------------------------


@pytest.mark.e2e
def test_the_golden_recording_replays_through_the_api_with_the_operators_approval(
    settings: Settings,
) -> None:
    """A real qwen3:8b two-specialist run, replayed offline from its committed recording;
    the operator approves the exact proposal through the API, as the owner did at the
    terminal (approving changes nothing the model saw)."""
    [golden] = [f for f in list_fixtures() if f.id == GOLDEN]
    with client_for(Runtime(settings)) as client:
        started = client.post(
            "/incidents/run",
            json={
                "scenario_id": GOLDEN_SCENARIO,
                "config": {"provider": "replay", "replay_fixture": GOLDEN},
            },
        )
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["labels"] == ["SIMULATED ENVIRONMENT", "REPLAY", "RECORDED REAL-LLM RUN"]
        run_id = body["run_id"]
        decide(client, pending_for(client, run_id), "approved")
        finished(client, run_id)
        run = views(client, run_id)
        pages = whole_timeline(client, run_id)
        stored = client.get(f"/runs/{run_id}/events").json()

    summary = run["/detail"]["summary"]
    assert (summary["state"], summary["outcome"]) == ("RESOLVED", "resolved"), summary["error"]
    assert (summary["provider_class"], summary["model"]) == ("replay", "qwen3:8b")
    assert summary["model_requests"] == 0  # no model was called in this run
    assert summary["modes"] == ["parallel", "parallel"]
    config = run["/detail"]["record"]["config"]  # the run asked what the original run asked
    assert (config["strategy"], config["use_memory"], config["retrieval"]) == (
        golden.strategy,
        golden.use_memory,
        golden.retrieval,
    )
    replay = summary["replay"]
    assert replay["fixture_id"] == GOLDEN and replay["committed_fixture"]
    assert replay["recording_matches_fixture"] is True
    assert replay["recording_id"] == golden.recording_id
    assert replay["original_export"] == golden.exported_run
    assert summary["labels"] == ["SIMULATED ENVIRONMENT", "REPLAY", "RECORDED REAL-LLM RUN"]

    timeline = run["/timeline"]
    assert [(r["round"], r["mode"]) for r in timeline["rounds"]] == [
        (1, "parallel"),
        (2, "parallel"),
    ]
    assert {(lane["agent"], lane["round"]) for lane in timeline["lanes"]} == {
        ("telemetry_investigator", 1),
        ("infrastructure_investigator", 1),
        ("telemetry_investigator", 2),
        ("infrastructure_investigator", 2),
    }
    assert REPLAY_NOTE in timeline["notes"] and CONCURRENCY_NOTE in timeline["notes"]
    assert [e["seq"] for e in pages] == [e["seq"] for e in stored]  # every event, once
    calls = [e for e in pages if e["kind"] == "model_request"]
    assert len(calls) == 14 and all(e["model_request"]["replayed"] for e in calls)

    [model] = run["/usage"]["models"]
    assert (model["provider"], model["model"], model["provider_class"]) == (
        "ollama",
        "qwen3:8b",
        "replay",
    )
    assert (model["requests"], model["replayed_requests"]) == (14, 14)
    assert model["input_tokens"] == 52585  # the original run's own counts
    assert model["limits"] is None  # today's settings say nothing about the recorded run

    evidence = run["/evidence"]
    agents = {(f["agent"], f["round"]) for f in evidence["findings"]}
    assert len(agents) == 4 and all(f["author"] == "replayed_model" for f in evidence["findings"])
    first_round = [v for v in evidence["verifications"] if v["round"] == 1]
    assert first_round and any(c["status"] != "supported" for c in first_round[0]["checks"])
    best = evidence["final_report"]["best"]
    assert (best["mechanism"], best["target_service"]) == ("resource_pressure", "inventory")
    assert {item["recorded_by"] for item in evidence["registry"]} >= {
        "telemetry_investigator",
        "infrastructure_investigator",
    }
    memory = run["/retrieval"]["memory"]
    assert memory["enabled"] and memory["write_partition"] is not None
    assert memory["searches"][0]["created_before"] is not None  # as the recording found it


# --- absent runs and missing traces ---------------------------------------------------------


def test_an_unknown_run_is_404_everywhere_and_a_missing_trace_is_said(settings: Settings) -> None:
    runtime = Runtime(settings)
    runtime.runs.save(
        RunRecord(
            run_id="run-notrace001",
            scenario_id="bad-deployment",
            config=RunConfig(),
            state=IncidentState.RESOLVED,
        )
    )
    with client_for(runtime) as client:
        for suffix in RUN_VIEWS:
            response = client.get(f"/runs/run-unknown01{suffix}")
            assert response.status_code == 404, suffix
            assert response.headers[SCHEMA_HEADER] == API_SCHEMA
        assert client.get("/runs/run-unknown01/stream").text == ""  # nothing to replay
        run = views(client, "run-notrace001")

    summary = run["/detail"]["summary"]
    assert summary["status"] == "finished" and summary["labels"] == []
    assert "provider_info" in summary["not_recorded"] and "history" in summary["not_recorded"]
    assert run["/timeline"]["trace_available"] is False
    assert run["/timeline"]["not_recorded"] == ["trace events"]
    assert run["/evidence"]["registry"] == []
    assert run["/retrieval"]["searches"] == [] and run["/usage"]["models"] == []


# --- GET /runs: filters and pagination -------------------------------------------------------


def stored_run(index: int, **fields: Any) -> RunRecord:
    values: dict[str, Any] = {
        "run_id": f"run-page{index:06d}",
        "scenario_id": "bad-deployment",
        "config": RunConfig(),
        "state": IncidentState.RESOLVED,
        "environment": EnvironmentInfo(
            kind="simulated", name="s", namespace="shop", is_simulated=True
        ),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
    }
    return RunRecord(**(values | fields))


def test_runs_are_filtered_and_paged_with_the_total_in_a_header(settings: Settings) -> None:
    runtime = Runtime(settings)
    replay = ProviderInfo(
        name="replay:ollama:qwen3:8b", kind="replay", origin="replay", model="qwen3:8b"
    )
    scripted = ProviderInfo(
        name="deterministic-scripted-policy", kind="scripted", origin="scripted"
    )
    for i in range(7):
        runtime.runs.save(stored_run(i, provider_info=scripted, provider=scripted.name))
    runtime.runs.save(
        stored_run(7, provider_info=replay, provider=replay.name, state=IncidentState.FAILED)
    )
    runtime.runs.save(stored_run(8, scenario_id="memory-caution", config=RunConfig(label="night")))
    with client_for(runtime) as client:
        first = client.get("/runs", params={"limit": 3})
        second = client.get("/runs", params={"limit": 3, "offset": 3})
        replays = client.get("/runs", params={"provider_class": "replay"})
        failed = client.get("/runs", params={"state": "FAILED", "provider": "qwen3:8b"})
        labelled = client.get("/runs", params={"label": "night", "scenario": "memory-caution"})
        nothing = client.get("/runs", params={"environment": "kind"})
        invalid = client.get("/runs", params={"provider_class": "gpt"})

    assert first.headers[TOTAL_HEADER] == "9" and first.headers[SCHEMA_HEADER] == API_SCHEMA
    page = [RunSummary.model_validate(item).run_id for item in first.json()]
    assert page == ["run-page000008", "run-page000007", "run-page000006"]  # newest first
    assert [r["run_id"] for r in second.json()] == [
        "run-page000005",
        "run-page000004",
        "run-page000003",
    ]
    assert [r["run_id"] for r in replays.json()] == ["run-page000007"]
    assert replays.headers[TOTAL_HEADER] == "1" and replays.json()[0]["labels"] == [
        "SIMULATED ENVIRONMENT",
        "REPLAY",
    ]
    assert [r["run_id"] for r in failed.json()] == ["run-page000007"]
    assert [r["run_id"] for r in labelled.json()] == ["run-page000008"]
    assert nothing.json() == [] and nothing.headers[TOTAL_HEADER] == "0"
    assert invalid.status_code == 422


# --- the event stream resumes without duplicates --------------------------------------------


@pytest.mark.e2e
def test_the_stream_resumes_after_the_last_event_id(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        run_id = start(client, approval_mode="auto")
        finished(client, run_id)
        total = len(client.get(f"/runs/{run_id}/events").json())

        def ids(**request: Any) -> list[int]:
            with client.stream("GET", f"/runs/{run_id}/stream", **request) as stream:
                assert stream.headers[SCHEMA_HEADER] == API_SCHEMA
                return stream_ids("".join(stream.iter_text()))

        assert ids() == list(range(1, total + 1))
        assert ids(headers={"Last-Event-ID": "10"}) == list(range(11, total + 1))
        both = ids(params={"after_seq": 5}, headers={"Last-Event-ID": "20"})
        assert both == list(range(21, total + 1))  # the later of the two wins
        assert ids(params={"after_seq": 12}) == list(range(13, total + 1))
        assert ids(headers={"Last-Event-ID": "not-a-seq"}) == list(range(1, total + 1))


@pytest.mark.e2e
def test_a_reconnecting_stream_of_a_live_run_gets_each_event_once(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        run_id = start(client, approval_mode="manual")
        pending = pending_for(client, run_id)
        seen = client.get(f"/runs/{run_id}/events").json()
        resume_from = seen[-1]["seq"] - 2  # the client had all but the last two events
        # The operator approves while the reconnected stream waits for live events.
        approver = threading.Timer(0.5, decide, args=(client, pending, "approved"))
        approver.start()
        try:
            headers = {"Last-Event-ID": str(resume_from)}
            with client.stream("GET", f"/runs/{run_id}/stream", headers=headers) as stream:
                text = "".join(stream.iter_text())
        finally:
            approver.join()
        total = len(client.get(f"/runs/{run_id}/events").json())

    got = stream_ids(text)
    assert got == list(range(resume_from + 1, total + 1))  # each event once, in order
    assert '"category":"run.summary"' in text


# --- the per-run provider choice --------------------------------------------------------------


def test_a_client_may_pick_only_the_providers_the_owner_allows(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:

        def post(scenario: str = "bad-deployment", **config: Any) -> httpx.Response:
            return client.post("/incidents/run", json={"scenario_id": scenario, "config": config})

        refused = {
            "paid": post(provider="anthropic"),
            "not allowed": post(provider="groq"),
            "unknown": post(provider="gpt-9"),
            "no fixture": post(provider="replay"),
            "unknown fixture": post(provider="replay", replay_fixture="../../etc/passwd"),
            "other scenario": post(provider="replay", replay_fixture=GOLDEN),
            "other routing": post(
                GOLDEN_SCENARIO, provider="replay", replay_fixture=GOLDEN, strategy="single"
            ),
            "no replay": post(provider="deterministic", replay_fixture=GOLDEN),
            "pace without replay": post(provider="deterministic", replay_pace=0.05),
            "pace out of range": post(
                GOLDEN_SCENARIO, provider="replay", replay_fixture=GOLDEN, replay_pace=2
            ),
        }
        default = start(client, approval_mode="auto")
        scripted = start(client, provider="deterministic", approval_mode="auto")
        replay = start(
            client,
            GOLDEN_SCENARIO,
            provider="replay",
            replay_fixture=GOLDEN,
            approval_mode="auto",
            replay_pace=0.0005,  # demo pacing: a little timing, the same replay
        )
        records = [finished(client, run_id)["record"] for run_id in (default, scripted, replay)]

    statuses = {name: response.status_code for name, response in refused.items()}
    assert statuses.pop("pace out of range") == 422  # the run settings' own range check
    details = {
        name: response.json()["detail"]
        for name, response in refused.items()
        if name != "pace out of range"
    }
    assert all(status == 400 for status in statuses.values()), details
    assert "replay_pace is only for the replay provider" in details["pace without replay"]
    assert "never selectable" in details["paid"]
    assert "api_selectable" in details["not allowed"]
    assert "not a configured provider" in details["unknown"]
    assert "needs replay_fixture" in details["no fixture"]
    assert "unknown replay fixture" in details["unknown fixture"]
    assert f"replays scenario '{GOLDEN_SCENARIO}'" in details["other scenario"]
    assert "strategy='parallel'" in details["other routing"]
    assert "only for the replay provider" in details["no replay"]
    assert [r["state"] for r in records] == ["RESOLVED", "RESOLVED", "RESOLVED"]
    assert records[2]["config"]["replay_fixture"] == GOLDEN
    assert records[2]["provider_info"]["origin"] == "replay"


def test_the_scripted_baseline_stays_selectable_when_a_model_is_the_default(
    settings: Settings,
) -> None:
    providers = settings.providers.model_copy(update={"default": "ollama"})
    runtime = Runtime(settings.model_copy(update={"providers": providers}))
    with client_for(runtime) as client:
        run_id = start(client, provider="deterministic", approval_mode="auto")
        record = finished(client, run_id)["record"]
    assert record["provider_info"]["origin"] == "scripted"


def test_a_model_provider_is_selectable_only_when_the_owner_marked_it(settings: Settings) -> None:
    runtime = Runtime(settings)
    allowed = check_run_request(runtime, "bad-deployment", RunConfig(provider="ollama"))
    assert allowed.provider == "ollama"  # configs/providers.yaml: api_selectable: true

    specs = dict(settings.providers.providers)
    specs["ollama"] = specs["ollama"].model_copy(update={"api_selectable": False})
    providers = settings.providers.model_copy(update={"providers": specs})
    locked = Runtime(settings.model_copy(update={"providers": providers}))
    with pytest.raises(RunRefused, match="api_selectable is off") as refused:
        check_run_request(locked, "bad-deployment", RunConfig(provider="ollama"))
    assert refused.value.status == 400


def test_a_replay_asks_exactly_what_its_recording_was_made_with(settings: Settings) -> None:
    """Routing, memory and retrieval come from the fixture's manifest entry (the original
    run's), whatever the server's defaults; the fields the client set stay marked as set."""
    runtime = Runtime(settings)
    config = RunConfig(provider="replay", replay_fixture="qwen3-8b-bad-deployment")

    checked = check_run_request(runtime, "bad-deployment", config)

    assert (checked.strategy, checked.use_memory, checked.retrieval) == ("adaptive", False, "bm25")
    assert {"strategy", "use_memory", "retrieval", "provider"} <= checked.model_fields_set
    assert "planner" not in checked.model_fields_set  # a scenario's own override still applies


def test_an_altered_recording_is_listed_as_altered_and_never_replayed(
    settings: Settings, tmp_path: Path
) -> None:
    folder = tmp_path / "recordings"
    folder.mkdir()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in manifest["fixtures"]:
        shutil.copy(MANIFEST.parent / entry["file"], folder / entry["file"])
    with (folder / "ollama-qwen3-8b-unfamiliar-parallel.jsonl").open("a") as handle:
        handle.write("\n")  # one byte more than the manifest pins
    (folder / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with client_for(Runtime(settings)) as client:
        client.app.state.reader.manifest = folder / "MANIFEST.json"  # type: ignore[attr-defined]
        listed = client.get("/replays").json()
        response = client.post(
            "/incidents/run",
            json={
                "scenario_id": GOLDEN_SCENARIO,
                "config": {"provider": "replay", "replay_fixture": GOLDEN},
            },
        )

    ReplaysView.model_validate(listed)
    fixtures = {f["id"]: f for f in listed["fixtures"]}
    assert fixtures[GOLDEN]["integrity"] == "altered" and not fixtures[GOLDEN]["launchable"]
    assert fixtures["qwen3-8b-bad-deployment"]["launchable"]
    assert response.status_code == 400 and "altered" in response.json()["detail"]


# --- providers, system, replays, evaluations --------------------------------------------------


def test_providers_show_classes_limits_and_who_may_pick_them(
    settings: Settings, no_ollama_probe: None
) -> None:
    with client_for(Runtime(settings)) as client:
        response = client.get("/providers")

    assert response.headers[SCHEMA_HEADER] == API_SCHEMA
    providers = {p["name"]: ProviderView.model_validate(p) for p in response.json()}
    assert providers["deterministic"].selectable and providers["deterministic"].default
    assert providers["ollama"].selectable and providers["ollama"].provider_class == "local_llm"
    assert providers["ollama"].limits.context_tokens == 16384
    groq = providers["groq"]
    assert not groq.selectable and groq.key_required and groq.key_present is False
    assert groq.limits.caps is not None and groq.limits.free_daily_request_limit is not None
    assert not providers["anthropic"].selectable
    assert "never selectable" in providers["anthropic"].selection_rule
    assert all(p.limitations for p in providers.values())


def test_a_live_adapter_is_operational_only_while_it_is_configured(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        simulated = SystemView.model_validate(client.get("/system").json())

    adapters = {a.kind: a for a in simulated.environment_adapters}
    assert adapters["simulated"].status == "active" and adapters["simulated"].operational
    # The Kind and SREGym adapters are built (M5), but each needs its live cluster: never
    # operational unless it is the configured adapter (GET /system never contacts a cluster).
    for kind, adapter in (("kind", "KubernetesEnvironment"), ("sregym", "SREGymEnvironment")):
        assert adapters[kind].status == "available" and not adapters[kind].operational
        assert adapters[kind].adapter == adapter and adapters[kind].milestone is None
    assert simulated.simulation_notice and simulated.label.startswith("SIMULATED")
    assert simulated.features.writes_require_operator_token
    assert "replay" in simulated.features.per_run_providers
    assert simulated.features.evaluation_modes["real_kubernetes"] == "implemented"


def test_replays_list_the_committed_recordings_with_their_checks(settings: Settings) -> None:
    with client_for(Runtime(settings)) as client:
        body = ReplaysView.model_validate(client.get("/replays").json())

    golden = next(f for f in body.fixtures if f.id == GOLDEN)
    assert golden.integrity == "intact" and golden.launchable
    assert golden.labels == ["RECORDED REAL-LLM RUN", "SIMULATED TARGET"]
    assert len(golden.sha256) == 12 and golden.scenario == GOLDEN_SCENARIO


def test_evaluation_batches_are_listed_by_source_and_read_by_id(settings: Settings) -> None:
    records = [
        EvalRecord(
            run_id="r1", mode="real_llm", scenario_id="s", success=True, oracle_verified=True
        ),
        EvalRecord(run_id="r2", mode="real_llm", scenario_id="s", failure="wrong_mechanism"),
    ]
    batch = EvalBatch(
        batch_id="2026-10-26-local",
        mode="real_llm",
        title="two runs",
        created_at=datetime(2026, 10, 26, tzinfo=UTC),
        records=records,
        limitations=["n = 2: not a success rate"],
    )
    root = settings.paths.data_dir / "evals" / "batches"
    write_batch(batch, root)
    (root / "half-written").mkdir()
    (root / "half-written" / "batch.json").write_text("{", encoding="utf-8")
    with client_for(Runtime(settings)) as client:
        listing = client.get("/evals")
        one = client.get("/evals/2026-10-26-local")
        missing = client.get("/evals/nope")
        legacy = client.get("/evals/results")

    view = EvalsView.model_validate(listing.json())
    [summary] = [b for b in view.batches if b.batch_id == "2026-10-26-local"]
    assert summary.source == "local" and summary.outcomes.successes == 1
    assert summary.outcomes.failures_by_category == {"wrong_mechanism": 1}
    assert {m.mode: m.status for m in view.modes}["real_kubernetes"] == "implemented"
    full = BatchView.model_validate(one.json())
    assert [r.run_id for r in full.records] == ["r1", "r2"]
    assert missing.status_code == 404
    assert set(legacy.json()) == {"running", "progress", "report", "error"}


def test_a_batch_view_carries_its_configurations_and_measured_comparisons(
    settings: Settings, tmp_path: Path
) -> None:
    """M6 W4: a batch planned with one-switch comparisons is read with them measured on its
    own records (evals/compare.py); GET /evals/components lists the published component
    reports, and "components" is never read as a batch id."""

    def run(label: str, scenario: str, success: bool) -> EvalRecord:
        return EvalRecord(
            run_id=f"{label}-{scenario}",
            mode="scripted_regression",
            scenario_id=scenario,
            label=label,
            strategy="single" if label == "single_only" else "parallel",
            success=success,
            oracle_verified=success,
            failure="" if success else "oracle_not_recovered",
        )

    batch = EvalBatch(
        batch_id="2026-11-08-w4-local",
        mode="scripted_regression",
        title="w4",
        created_at=datetime(2026, 11, 8, tzinfo=UTC),
        records=[run("single_only", "s1", False), run("parallel_only", "s1", True)],
        configurations=[
            EvalConfiguration(name="single_only", suite="test", switches={"strategy": "single"}),
            EvalConfiguration(
                name="parallel_only", suite="test", switches={"strategy": "parallel"}
            ),
        ],
        comparisons=[
            Comparison(
                name="single-vs-parallel",
                baseline="single_only",
                variant="parallel_only",
                switch="strategy",
                question="q",
            )
        ],
        scenario_sets={"test": ["s1"]},
    )
    write_batch(batch, settings.paths.data_dir / "evals" / "batches")
    report = ComponentReport(
        report_id="c1", created_at=datetime(2026, 11, 8, tzinfo=UTC), code_version="abc"
    )
    write_report(report, tmp_path / "components" / "c1")
    with client_for(Runtime(settings)) as client:
        client.app.state.reader.components_root = tmp_path / "components"  # type: ignore[attr-defined]
        full = BatchView.model_validate(client.get("/evals/2026-11-08-w4-local").json())
        components = ComponentsView.model_validate(client.get("/evals/components").json())

    assert [c.name for c in full.configurations] == ["single_only", "parallel_only"]
    [compared] = full.comparisons
    assert (compared.n, compared.baseline_successes, compared.variant_successes) == (1, 0, 1)
    assert compared.only_variant == ["s1"] and compared.unexpected == []
    assert full.scenario_sets == {"test": ["s1"]}
    assert [r.report_id for r in components.reports] == ["c1"]


# --- what a response must never carry ---------------------------------------------------------


@pytest.mark.e2e
def test_no_response_carries_a_token_key_capability_or_local_path(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, no_ollama_probe: None
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)  # a key-shaped secret on the server
    runtime = Runtime(settings)
    model = FakeOllama(responder=ScriptedModel())

    def make(config: RunConfig) -> Any:
        # A model run records its exchanges under the data folder, which is outside the
        # repository here: its record names that file by an absolute path.
        return create_provider(
            "ollama", runtime.settings, mock_http=model.mock(), ledger=runtime.ledger
        )

    runtime.make_provider = make  # type: ignore[method-assign]
    with client_for(runtime) as client:
        run_id = start(client, approval_mode="manual", use_memory=False)
        pending = pending_for(client, run_id)
        waiting = client.get("/approvals/pending").text  # holds the whole proposal
        decide(client, pending, "approved")
        assert finished(client, run_id)["record"]["state"] == "RESOLVED"
        stored = runtime.runs.get(run_id)
        assert stored is not None and stored.provider_info is not None
        recording = stored.provider_info.recording or ""
        assert recording.startswith(str(settings.paths.data_dir))  # the case to scrub
        paths = [
            "/health",
            "/system",
            "/providers",
            "/replays",
            "/evals",
            "/evals/results",
            "/runs",
            "/approvals/events",
            f"/approvals/{pending['proposal_id']}/events",
            "/memory",
            "/scenarios",
            "/knowledge/docs",
            "/knowledge/search?q=rollback",
            f"/runs/{run_id}/events",
            *(f"/runs/{run_id}{suffix}" for suffix in RUN_VIEWS),
        ]
        bodies = [waiting, *(client.get(path).text for path in paths)]
        with client.stream("GET", f"/runs/{run_id}/stream") as stream:
            bodies.append("".join(stream.iter_text()))
        spec = client.get("/openapi.json").json()

    text = "\n".join(bodies)
    for secret in (
        OPERATOR_TOKEN,
        FAKE_GROQ_KEY,
        str(settings.paths.data_dir),
        str(settings.paths.root),
        str(Path.home()),
    ):
        found = secret in text  # a boolean, so a failure never prints the secret
        assert not found
    capability = re.search(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", text)
    assert capability is None  # no signed capability, ever
    assert '"capability"' not in text and "Bearer " not in text
    assert '"key_present":true' in text.replace(" ", "")  # only whether a key is set
    schemas = spec["components"]["schemas"]
    for name in ("RunSummary", "RunDetail", "TimelineView", "EvidenceView", "UsageView"):
        assert name in schemas  # GET /openapi.json documents the contracts
