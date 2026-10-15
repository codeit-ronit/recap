"""The API reports what really happened: evaluation results, run status, environment label.

No real evaluation runs here: `run_evaluation` is replaced by small fakes, so these tests
check the API's own bookkeeping and stay fast.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api import service
from apps.api.main import create_app
from apps.api.service import RunManager
from evals.runner import EvalReport, Split
from recap.core.config import EnvironmentConfig, Settings
from recap.core.ids import utcnow
from recap.core.models import IncidentState, RunConfig, RunRecord, TraceEvent
from recap.runtime import Runtime
from recap.telemetry.tracer import TraceCategory
from tests.helpers import OPERATOR_HEADERS

TRAIN_LINE = "  train  train-checkout-bad-release         RESOLVED"


def wait_for(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    """Poll `predicate` until it returns something truthy, and return that."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def finished_results(client: TestClient) -> dict[str, Any] | None:
    body = client.get("/evals/results").json()
    return None if body["running"] else body


@pytest.fixture
def committed_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A stand-in for the committed evals/reports/latest/results.json."""
    latest = tmp_path / "latest"
    latest.mkdir()
    report = {"report_id": "committed"}
    (latest / "results.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(service, "LATEST_DIR", latest)
    monkeypatch.setattr(service, "new_report_dir", lambda: tmp_path / "eval")
    return report


# --- evaluations ----------------------------------------------------------------------------


def test_an_unknown_configuration_name_is_a_400_and_starts_nothing(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        response = client.post("/evals/run", json={"configs": ["nope"]})
        assert response.status_code == 400
        assert "nope" in response.json()["detail"]
        assert client.app.state.manager.eval_task is None  # type: ignore[attr-defined]


def test_results_show_the_committed_report_until_this_server_runs_one(
    settings: Settings, committed_report: dict[str, Any]
) -> None:
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        body = client.get("/evals/results").json()
    assert body == {"running": False, "progress": [], "report": committed_report, "error": None}


def test_a_failed_evaluation_reports_its_error_instead_of_an_older_report(
    settings: Settings, committed_report: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_evaluation(
        out_dir: Path, *, config_names: list[str] | None, progress: Callable[[str], None]
    ) -> EvalReport:
        progress(TRAIN_LINE)
        raise RuntimeError("simulator crashed")

    monkeypatch.setattr(service, "run_evaluation", failing_evaluation)
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        assert client.post("/evals/run", json={}).status_code == 202
        body = wait_for(lambda: finished_results(client))
    assert body == {
        "running": False,
        "progress": [TRAIN_LINE],
        "report": None,
        "error": "RuntimeError: simulator crashed",
    }


def test_results_follow_this_servers_evaluation_while_it_runs_and_when_it_is_done(
    settings: Settings, committed_report: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()  # set by this thread; the fake runs on the server's event loop
    report = EvalReport(
        report_id="this-server",
        created_at="2026-01-01T00:00:00+00:00",
        recap_version="0.1.0",
        git_commit="unknown",
        config_hash="config",
        fixture_hash="fixtures",
        provider="deterministic-scripted-policy",
        split=Split(name="tiny", train=[], test=[]),
        latency={"read_ms": 0, "write_ms": 0},
        train=[],
        cases=[],
        summaries=[],
        caveats=[],
    )

    async def slow_evaluation(
        out_dir: Path, *, config_names: list[str] | None, progress: Callable[[str], None]
    ) -> EvalReport:
        while not release.is_set():
            await asyncio.sleep(0.01)
        return report

    monkeypatch.setattr(service, "run_evaluation", slow_evaluation)
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        try:
            started = client.post("/evals/run", json={"configs": ["baseline_single"]})
            assert started.json()["status"] == "started"
            running = client.get("/evals/results").json()
            assert running["running"] is True
            assert running["report"] is None  # the committed report is not this run's result
        finally:
            release.set()
        body = wait_for(lambda: finished_results(client))
    assert body["report"]["report_id"] == "this-server"
    assert body["error"] is None


# --- run status -----------------------------------------------------------------------------


def save_run(runtime: Runtime, run_id: str, state: IncidentState) -> None:
    """Store a run checkpoint the way another process (e.g. the CLI demo) would."""
    record = RunRecord(run_id=run_id, scenario_id="bad-deployment", config=RunConfig(), state=state)
    runtime.runs.save(record)


def test_a_run_from_another_process_is_external_until_it_reaches_a_terminal_state(
    settings: Settings,
) -> None:
    runtime = Runtime(settings)
    save_run(runtime, "run-cli-waiting", IncidentState.AWAITING_APPROVAL)
    save_run(runtime, "run-cli-done", IncidentState.RESOLVED)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        assert client.get("/runs/run-cli-waiting").json()["status"] == "external"
        assert client.get("/runs/run-cli-done").json()["status"] == "finished"
        assert client.get("/runs/run-nope").status_code == 404


async def test_the_stream_of_an_external_run_ends_after_its_stored_events(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "KEEPALIVE_SECONDS", 60)  # waiting for live events would time out
    manager = RunManager(Runtime(settings))
    save_run(manager.runtime, "run-cli", IncidentState.AWAITING_APPROVAL)
    stored = TraceEvent(
        event_id="evt-1",
        run_id="run-cli",
        seq=1,
        stage="AWAITING_APPROVAL",
        ts=utcnow(),
        category=TraceCategory.STATE,
    )
    manager.runtime.events.write(stored)

    async def collect() -> list[str]:
        return [frame async for frame in manager.stream("run-cli")]

    frames = await asyncio.wait_for(collect(), timeout=5)
    assert len(frames) == 1 and frames[0].startswith("id: 1\nevent: trace\n")


# --- environment label ----------------------------------------------------------------------


def test_the_health_label_says_simulated_only_for_a_simulated_environment(
    settings: Settings,
) -> None:
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        assert client.get("/health").json()["label"] == "SIMULATED LOCAL ENVIRONMENT · V1"
    sregym = settings.model_copy(update={"environment": EnvironmentConfig(kind="sregym")})
    with TestClient(create_app(Runtime(sregym)), headers=OPERATOR_HEADERS) as client:
        assert client.get("/health").json()["label"] == "LIVE ENVIRONMENT (sregym) · V1"
