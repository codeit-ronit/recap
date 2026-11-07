"""Several incidents at once through the HTTP API (POST /incidents/run), offline.

The API starts every run as its own background task (RunManager in apps/api/service.py) and
sets no limit on how many run together. On the simulator each run has its own sandbox,
incident, proposal and approval request, so runs started together finish as they would
alone, and a decision on one proposal can never decide another's. A live target has one
namespace, leased by one run at a time (the Kind adapter's NamespaceLease): a second run there
is refused at its reset, before it changes anything, and the API reports the refusal as that
run's error while the first run goes on undisturbed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from recap.core.config import Settings
from recap.environments.registry import create_environment
from recap.runtime import Runtime
from tests.e2e.test_api import wait_for
from tests.e2e.test_kind_pipeline import adapter, kind_runtime
from tests.helpers import OPERATOR_HEADERS

pytestmark = pytest.mark.e2e

# Four simulator incidents, each with its own target and fix (what the scripted policy does).
SCENARIOS = {
    "bad-deployment": ("execute_rollback", "checkout"),
    "resource-pressure": ("restart_service", "inventory"),
    "test-frontend-bad-release": ("execute_rollback", "frontend"),
    "train-cart-memory-leak": ("restart_service", "cart"),
}
MANUAL = {"approval_mode": "manual", "memory_write": False}


def start(client: TestClient, scenario: str, config: dict[str, Any]) -> str:
    response = client.post("/incidents/run", json={"scenario_id": scenario, "config": config})
    assert response.status_code == 202, response.text
    return str(response.json()["run_id"])


def decide(client: TestClient, request: dict[str, Any], **extra: str) -> Any:
    body = {"decision": "approved", "proposal_digest": request["proposal_digest"], **extra}
    return client.post(f"/approvals/{request['proposal_id']}", json=body)


def test_concurrent_simulated_runs_finish_independently(settings: Settings) -> None:
    """Four runs started together, all four waiting for a person at the same moment. A decision
    that pairs one run's proposal with another run's digest is refused (409) and decides
    nothing. Each request is then approved by its own proposal ID and digest: all four
    resolve, each with exactly its own incident's fix, under its own approval, written once
    into its own sandbox."""
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        started = {start(client, scenario, MANUAL): scenario for scenario in SCENARIOS}

        def all_waiting() -> list[dict[str, Any]] | None:
            pending = client.get("/approvals/pending").json()
            return pending if len(pending) == len(SCENARIOS) else None

        pending = wait_for(all_waiting)
        requests = {request["run_id"]: request for request in pending}
        assert set(requests) == set(started)  # one request per run, all open together
        first, second = pending[0], pending[1]
        crossed = decide(client, {**first, "proposal_digest": second["proposal_digest"]})
        assert crossed.status_code == 409 and "digest mismatch" in crossed.json()["detail"]
        assert len(client.get("/approvals/pending").json()) == len(SCENARIOS)
        for request in pending:
            assert decide(client, request).status_code == 200

        def all_finished() -> list[dict[str, Any]] | None:
            details = [client.get(f"/runs/{run_id}").json() for run_id in started]
            return details if all(d["status"] == "finished" for d in details) else None

        details = wait_for(all_finished)

    for detail in details:
        record = detail["record"]
        run_id, request = record["run_id"], requests[record["run_id"]]
        assert record["state"] == "RESOLVED", record["outcome_note"]
        [action] = record["actions"]
        assert (action["action"], action["target"]) == SCENARIOS[started[run_id]]
        assert action["result"] == "recovered"
        [approval] = record["approvals"]
        assert approval["proposal_id"] == request["proposal_id"]
        assert approval["proposal_digest"] == request["proposal_digest"]
        assert approval["incident_id"] == record["incident"]["incident_id"]
        assert approval["consumed_at"] is not None
        sandbox = create_environment(settings.environment, run_id)
        [write] = sandbox.store.load()["action_log"]  # type: ignore[attr-defined]
        assert write["target"] == action["target"] and write["ok"] is True
        assert write["approval_id"] == approval["approval_id"]


def test_a_second_kind_run_is_refused_while_the_first_holds_the_namespace(
    settings: Settings, tmp_path: Path
) -> None:
    """On the Kind fake, the first run resets the namespace and waits for its approval, holding
    the lease. The second is accepted (202), then refused by the lease at its reset: its only
    cluster call was the preflight's version read, it changed nothing, asked nobody, and the
    API reports it as status 'error' naming the run that holds the namespace. The first run
    is undisturbed: approved by its operator, who types the target's name as a live target
    requires, it resolves and gives the namespace back."""
    runtime, _ = kind_runtime(settings, tmp_path)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        first = start(client, "kind-bad-release", {"memory_write": False})
        [request] = wait_for(lambda: client.get("/approvals/pending").json())
        assert request["run_id"] == first

        second = start(client, "kind-bad-release", {"memory_write": False})

        def refused() -> dict[str, Any] | None:
            body = client.get(f"/runs/{second}").json()
            return body if body["status"] == "error" else None

        body = wait_for(refused)
        assert body["error"].startswith("LeaseHeld: namespace recap-demo is leased by run ")
        assert first in body["error"] and body["record"] is None
        assert [p["run_id"] for p in client.get("/approvals/pending").json()] == [first]
        local = adapter(runtime, second)
        assert [(c["op"], c["identity"]) for c in local.state.commands()] == [("version", "reader")]
        assert local.state.writes() == []
        holder = local.lease.holder("recap-demo")
        assert holder is not None and holder.run_id == first

        approved = decide(client, request, confirm_target=request["proposal"]["target"])
        assert approved.status_code == 200, approved.text

        def finished() -> dict[str, Any] | None:
            body = client.get(f"/runs/{first}").json()
            return body if body["status"] == "finished" else None

        record = wait_for(finished)["record"]

    assert record["state"] == "RESOLVED", record["outcome_note"]
    assert [(a["action"], a["target"]) for a in record["actions"]] == [
        ("execute_rollback", "backend")
    ]
    assert adapter(runtime, first).lease.holder("recap-demo") is None
