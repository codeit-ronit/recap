"""The HTTP API drives a real incident run, including a human approval, end to end."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from apps.api.main import create_app
from recap.core.config import Settings
from recap.runtime import Runtime


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.1):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def test_dashboard_flow_over_http(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        assert client.get("/health").json()["label"].startswith("SIMULATED")
        assert any(s["id"] == "bad-deployment" for s in client.get("/scenarios").json())
        assert client.get("/system").json()["environment"]["is_simulated"] is True

        run_id = client.post(
            "/incidents/run",
            json={"scenario_id": "bad-deployment", "config": {"approval_mode": "manual"}},
        ).json()["run_id"]

        pending = wait_for(lambda: client.get("/approvals/pending").json())[0]
        # Approving a different proposal (wrong digest) is refused.
        wrong = client.post(
            f"/approvals/{pending['proposal_id']}",
            json={"decision": "approved", "proposal_digest": "f" * 64},
        )
        assert wrong.status_code == 409
        ok = client.post(
            f"/approvals/{pending['proposal_id']}",
            json={"decision": "approved", "proposal_digest": pending["proposal_digest"]},
        )
        assert ok.status_code == 200 and ok.json()["decision"] == "approved"

        def finished():  # type: ignore[no-untyped-def]
            body = client.get(f"/runs/{run_id}").json()
            return body if body["status"] == "finished" else None

        body = wait_for(finished)
        assert body["record"]["state"] == "RESOLVED"
        assert body["evidence"], "evidence is persisted and served"

        events = client.get(f"/runs/{run_id}/events").json()
        assert events[-1]["category"] == "run.summary"
        with client.stream("GET", f"/runs/{run_id}/stream") as stream:
            text = "".join(stream.iter_text())
        assert "event: trace" in text and "run.summary" in text

        assert client.get("/memory").json(), "a verified episode was consolidated"
        hits = client.get("/knowledge/search", params={"q": "rollback after release"}).json()
        assert hits and hits[0]["chunk"]["doc_path"].startswith("fixtures/knowledge/")
        assert client.get("/runs").json()[0]["run_id"] == run_id


def test_api_refuses_to_disable_safety(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.post(
            "/incidents/run",
            json={"scenario_id": "bad-deployment", "config": {"safety_enabled": False}},
        )
        assert response.status_code == 400
        assert client.post("/incidents/run", json={"scenario_id": "nope"}).status_code == 404


def test_api_refuses_to_switch_the_provider(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.post(
            "/incidents/run",
            json={"scenario_id": "bad-deployment", "config": {"provider": "anthropic"}},
        )
        assert response.status_code == 400
