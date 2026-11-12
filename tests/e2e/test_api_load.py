"""The API load test (scripts/api_load_test.py), run small: runs started at once, each
approved through POST /approvals by its own operator, finish independently, each with its own
record, and the server's metrics agree with what the operators saw."""

from __future__ import annotations

import json

import pytest

from apps.api.main import create_app
from recap.core.config import Settings
from recap.runtime import Runtime
from scripts.api_load_test import (
    LoadTestRefused,
    TimedClient,
    _mib,
    level_report,
    load_test,
    main,
)
from tests.helpers import OPERATOR_TOKEN


@pytest.mark.e2e
async def test_concurrent_runs_each_finish_with_their_own_record_and_approval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_METRICS", "1")
    runtime = Runtime(settings)

    report = await load_test(create_app(runtime), runs=3, token=OPERATOR_TOKEN, timeout=120)

    assert report["ok"], report
    runs = report["runs"]
    assert [run["state"] for run in runs] == ["RESOLVED"] * 3
    assert len({run["run_id"] for run in runs}) == 3
    assert len({run["approved_proposal"] for run in runs}) == 3
    for run in runs:
        assert run["own_record"] and run["writes"] == 1 and run["error"] is None
        assert run["seconds"] > 0
        # Checked again in the stores, not only through the API: the run's one approval is
        # the one its operator gave, for a request this run made, and it was used once.
        record = runtime.runs.get(run["run_id"])
        assert record is not None
        [approval] = record.approvals
        assert approval.proposal_id == run["approved_proposal"]
        assert approval.consumed_at is not None
        request = runtime.approvals.get_request(approval.proposal_id)
        assert request is not None and request.run_id == run["run_id"]
        assert [proposal.proposal_id for proposal in record.proposals] == [approval.proposal_id]

    requests = report["requests"]
    assert requests["errors_5xx"] == 0
    by_route = requests["by_route"]
    assert by_route["POST /incidents/run"]["count"] == 3
    assert by_route["POST /approvals/{proposal_id}"]["count"] == 3
    for stats in [requests["latency_ms"], report["run_seconds"], *by_route.values()]:
        assert 0 <= stats["p50"] <= stats["p95"] <= stats["p99"] <= stats["max"]

    # What the server counted (GET /metrics) agrees with what the operators saw.
    server = report["server_metrics"]
    assert server["api_requests"] == requests["total"] and server["api_5xx"] == 0
    assert server["incidents_finished"] == {"RESOLVED": 3}
    assert server["active_incidents"] == 0
    assert server["actions"] == {"executed": 3}
    assert server["recovery_verified"] == {"true": 3}
    assert server["model_requests"] == 0  # the scripted baseline is not a model


async def test_the_load_test_never_starts_a_run_on_a_live_target(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(settings)
    info = runtime.environment_info().model_copy(update={"is_simulated": False, "kind": "kind"})
    monkeypatch.setattr(runtime, "environment_info", lambda: info)

    with pytest.raises(LoadTestRefused, match="never starts runs on a live target"):
        await load_test(create_app(runtime), runs=2, token=OPERATOR_TOKEN)

    assert runtime.runs.list(10) == []  # nothing was started


@pytest.mark.e2e
def test_the_script_uses_the_simulator_whatever_the_shell_says_and_prints_its_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RECAP_TOOL_LATENCY_MS", "0")
    monkeypatch.setenv("RECAP_TARGET", "kind-demo")  # ignored: the script reads configs/ itself
    monkeypatch.setenv("RECAP_METRICS", "0")  # the script turns it on; restored afterwards

    code = main(["--runs", "2", "--timeout", "120"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["ok"]
    assert report["states"] == {"RESOLVED": 2}
    assert report["provider"] == "deterministic"
    assert report["server_metrics"]["incidents_finished"] == {"RESOLVED": 2}
    assert "SIMULATED" in report["what"]


def test_a_level_counts_a_full_queue_as_backpressure_never_as_a_failure() -> None:
    """--url mode (M6 W6): a submission the queue refuses with 429 is its backpressure working;
    a 503 (Redis away) fails the level; docker's sizes read as MiB."""
    good = {
        "run_id": "run-1",
        "state": "RESOLVED",
        "seconds": 2.0,
        "queue_seconds": 0.5,
        "client_seconds": 2.5,
        "own_record": True,
        "error": None,
    }
    full = {**good, "run_id": None, "state": None, "refused": 429}
    timed = TimedClient(client=None)  # type: ignore[arg-type]  (nothing is sent)
    level = level_report(2, [good, full], timed, 4.0, None)
    assert level["ok"] and level["accepted"] == 1 and level["refused"] == {"429": 1}
    assert level["throughput_runs_per_second"] == 0.25
    assert level["queue_wait_seconds"]["p50"] == 0.5
    assert not level_report(2, [good, {**full, "refused": 503}], timed, 4.0, None)["ok"]
    assert (_mib("1.5GiB"), _mib("512KiB"), _mib("12.5MiB"), _mib("??")) == (1536.0, 0.5, 12.5, 0)
