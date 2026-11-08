"""The published real-cluster runs through the API, on a fresh data folder (a fresh checkout):
GET /published lists them, every run view opens them read-only, and nothing can act on them.

The data folder is empty (the `settings` fixture), no cluster, model or network is involved,
and the operator token is sent on every request, so a refusal below is the read-only rule's,
never a missing sign-in.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.service import RunReader
from apps.api.views import PUBLISHED_LABEL
from evals.published import LIVE_DIR, MANIFEST
from recap.core.config import Settings
from recap.core.models import RunRecord
from recap.runtime import Runtime
from tests.helpers import OPERATOR_HEADERS

SREGYM = "run-9d7f47104d"
SREGYM_FOLDER = "2026-11-07-qwen3-8b-sregym-human-approved-prompts-v4.7"
KIND = ("run-047aac6eb0", "run-f5b9e5bf45", "run-34a3580d2f")


@pytest.fixture
def runtime(settings: Settings) -> Runtime:
    return Runtime(settings)


@pytest.fixture
def client(runtime: Runtime) -> Iterator[TestClient]:
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        yield client


def trace_lines(folder: str) -> list[dict[str, Any]]:
    path = LIVE_DIR / folder / "trace.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_published_runs_are_listed_apart_from_this_servers_own_runs(client: TestClient) -> None:
    body = client.get("/published").json()
    assert body["schema_version"] == "recap-api-v1"
    assert body["manifest_schema"] == "recap-published-runs-v1"
    assert body["manifest_error"] is None and body["rejected"] == []
    assert [run["run_id"] for run in body["runs"]] == [*KIND, SREGYM]
    for run in body["runs"]:
        assert run["status"] == "published"
        assert run["labels"][0] == PUBLISHED_LABEL
        assert run["environment_simulated"] is False
        assert run["provider_class"] == "local_llm" and run["model"] == "qwen3:8b"
        provenance = run["published"]
        assert provenance["integrity"] == "verified"
        assert provenance["folder"].startswith("evals/live/")
        assert provenance["files"] == ["README.md", "recording.jsonl", "run.json", "trace.jsonl"]
    assert len(body["records_without_trace"]) == 8
    assert {r["final_state"] for r in body["records_without_trace"]} == {"FAILED", "BLOCKED"}
    assert body["limitations"] and all(isinstance(text, str) for text in body["limitations"])
    # This server's own runs (none on a fresh data folder) are a different list.
    runs = client.get("/runs")
    assert runs.json() == [] and runs.headers["X-Total-Count"] == "0"


def test_a_published_run_opens_in_every_run_view(client: TestClient) -> None:
    detail = client.get(f"/runs/{SREGYM}").json()
    assert detail["status"] == "published"
    assert detail["summary"]["state"] == "RESOLVED"
    assert detail["summary"]["published"]["target"] == "sregym"
    assert detail["record"]["run_id"] == SREGYM
    assert detail["evidence"] == []  # not exported: said so, never invented

    events = trace_lines(SREGYM_FOLDER)
    timeline = client.get(f"/runs/{SREGYM}/timeline?limit=2000").json()
    assert timeline["status"] == "published"
    assert [entry["seq"] for entry in timeline["entries"]] == [e["seq"] for e in events]

    evidence = client.get(f"/runs/{SREGYM}/evidence").json()
    assert evidence["unknown_cited_ids"] == []  # no false "not in the registry" finding
    assert any("not published with this run" in item for item in evidence["not_recorded"])
    assert evidence["proposals"] and evidence["policy_verdicts"]

    assert client.get(f"/runs/{SREGYM}/retrieval").status_code == 200
    assert client.get(f"/runs/{SREGYM}/usage").json()["status"] == "published"
    assert len(client.get(f"/runs/{SREGYM}/events").json()) == len(events)
    assert len(client.get(f"/runs/{SREGYM}/events?after_seq=50").json()) == len(events) - 50

    stream = client.get(f"/runs/{SREGYM}/stream")
    assert stream.status_code == 200
    assert stream.text.count("event: trace") == len(events)  # its trace, then the end


def test_sregyms_grader_and_recaps_oracle_stay_apart(client: TestClient) -> None:
    runs = {run["run_id"]: run for run in client.get("/published").json()["runs"]}
    sregym = runs[SREGYM]
    assert sregym["oracle"]["verified"] is True  # RECAP's own oracle
    assert sregym["published"]["benchmark_verdict"] == "pass"  # SREGym's grader, by hand
    assert "run-oracle.py" in sregym["published"]["benchmark"]
    assert any("Not a held-out benchmark result" in note for note in sregym["published"]["notes"])
    for run_id in KIND:
        assert runs[run_id]["published"]["benchmark_verdict"] is None
        assert runs[run_id]["published"]["benchmark"] is None
    graded = {
        r["run_id"]: r["benchmark_verdict"]
        for r in client.get("/published").json()["records_without_trace"]
    }
    assert graded["run-e08c17d46e"] == "fail"


def test_nothing_can_act_on_a_published_run(client: TestClient, runtime: Runtime) -> None:
    memories = runtime.memory.count()
    record = client.get(f"/runs/{SREGYM}").json()["record"]
    proposal = record["proposals"][0]
    approval = record["approvals"][0]

    cancel = client.post(f"/runs/{SREGYM}/cancel")
    assert cancel.status_code == 409 and "published historical run" in cancel.json()["detail"]

    # Its approval was a decision about that run, then: it is no permission now.
    decide = client.post(
        f"/approvals/{proposal['proposal_id']}",
        json={"decision": "approved", "proposal_digest": "0" * 64, "confirm_target": "geo"},
    )
    assert decide.status_code == 404
    assert runtime.approvals.get_request(proposal["proposal_id"]) is None
    assert runtime.approvals.get_approval(approval["approval_id"]) is None
    assert client.get(f"/approvals/{proposal['proposal_id']}/events").json() == []

    # Its recording is evidence, not a replay fixture.
    started = client.post(
        "/incidents/run",
        json={
            "scenario_id": "bad-deployment",
            "config": {"provider": "replay", "replay_fixture": "20261107-054426-ollama-vm-cc678a"},
        },
    )
    assert started.status_code in (400, 404, 422)

    # Reading it changed no store of this server.
    assert runtime.runs.list() == []
    assert runtime.events.list_events(SREGYM) == []
    assert runtime.approvals.pending() == []
    assert runtime.memory.count() == memories


def test_a_tampered_artifact_is_never_served(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    shutil.copy(MANIFEST, live / "MANIFEST.json")
    for entry in json.loads(MANIFEST.read_text())["runs"]:
        shutil.copytree(LIVE_DIR / entry["folder"], live / entry["folder"])
    run_json = live / SREGYM_FOLDER / "run.json"
    run_json.write_text(run_json.read_text().replace('"RESOLVED"', '"FAILED"', 1))
    app: Any = client.app
    app.state.reader = RunReader(app.state.manager, published_manifest=live / "MANIFEST.json")

    body = client.get("/published").json()
    assert [run["run_id"] for run in body["runs"]] == list(KIND)
    assert body["rejected"] == [
        {
            "folder": SREGYM_FOLDER,
            "run_id": SREGYM,
            "reason": "run.json does not match the SHA-256 and size the manifest pins "
            "(altered or replaced)",
        }
    ]
    assert client.get(f"/runs/{SREGYM}").status_code == 404
    assert client.get(f"/runs/{SREGYM}/events").json() == []


def test_this_servers_own_copy_of_a_run_wins(client: TestClient, runtime: Runtime) -> None:
    published = client.get(f"/runs/{SREGYM}").json()["record"]
    runtime.runs.save(RunRecord.model_validate(published))
    detail = client.get(f"/runs/{SREGYM}").json()
    assert detail["status"] == "finished"
    assert detail["summary"]["published"] is None
    assert PUBLISHED_LABEL not in detail["summary"]["labels"]
