"""The API's security controls, signed in and not (audit finding 2.4; see apps/api/auth.py).

The audit reproductions in test_audit_api_security.py show that anonymous callers are now
refused. These tests cover the rest: a wrong token, the identity a decision records, the
origin and host checks, the decision log endpoint, cancellation, and the token file.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import OperatorAuth, origin_allowed
from apps.api.main import create_app
from recap.core.config import Settings
from recap.core.models import ApprovalRequest, ApprovalStatus, MitigationProposal
from recap.runtime import Runtime
from tests.helpers import OPERATOR_HEADERS

WRITE_ENDPOINTS = [
    ("/incidents/run", {"scenario_id": "bad-deployment"}),
    ("/approvals/prop-x", {"decision": "approved", "proposal_digest": "0" * 64}),
    ("/evals/run", {}),
    ("/runs/run-x/cancel", None),
]


def open_request(runtime: Runtime, proposal_id: str = "prop-sec-001") -> ApprovalRequest:
    proposal = MitigationProposal(
        proposal_id=proposal_id,
        incident_id="inc-sec-001",
        action="execute_rollback",
        target="checkout",
        params={"to_version": "v2.4.0"},
        rationale="errors started right after v2.4.1",
        evidence_ids=["ev-1-001"],
    )
    return runtime.approvals.create_request(proposal, run_id="run-sec-001", ttl_seconds=900)


def wait_for(predicate, timeout: float = 60.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.mark.parametrize(("path", "body"), WRITE_ENDPOINTS, ids=[p for p, _ in WRITE_ENDPOINTS])
def test_a_wrong_token_is_refused_on_every_state_changing_endpoint(
    settings: Settings, path: str, body: dict | None
) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        response = client.post(path, json=body, headers={"Authorization": "Bearer not-the-token"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_session_endpoint_names_the_operator(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings))) as client:
        anonymous = client.get("/auth/session")
        signed_in = client.get("/auth/session", headers=OPERATOR_HEADERS)

    assert anonymous.status_code == 401
    assert signed_in.json() == {"operator": "local-operator", "actor": "operator:local-operator"}


def test_a_decision_records_the_authenticated_operator(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECAP_OPERATOR_NAME", "jamie")
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        body = {"decision": "approved", "proposal_digest": request.proposal_digest}
        decided = client.post(f"/approvals/{request.proposal_id}", json=body)
        log = client.get(f"/approvals/{request.proposal_id}/events").json()
        recent = client.get("/approvals/events", params={"limit": 1}).json()

    assert decided.status_code == 200 and decided.json()["actor"] == "operator:jamie"
    assert [(e["kind"], e["actor"]) for e in log] == [
        ("requested", "recap"),
        ("approved", "operator:jamie"),
    ]
    assert recent[0]["kind"] == "approved"


def test_a_body_that_names_its_own_actor_is_rejected(settings: Settings) -> None:
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        body = {
            "decision": "approved",
            "proposal_digest": request.proposal_digest,
            "actor": "incident-commander",
        }
        response = client.post(f"/approvals/{request.proposal_id}", json=body)

    assert response.status_code == 422  # an unknown field is an error, not silently ignored
    assert runtime.approvals.get_request(request.proposal_id).status == ApprovalStatus.PENDING


def test_a_rejection_through_the_api_revokes_an_unused_approval(settings: Settings) -> None:
    runtime = Runtime(settings)
    request = open_request(runtime)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        url = f"/approvals/{request.proposal_id}"
        digest = request.proposal_digest
        client.post(url, json={"decision": "approved", "proposal_digest": digest})
        revoked = client.post(url, json={"decision": "rejected", "proposal_digest": digest})
        again = client.post(url, json={"decision": "approved", "proposal_digest": digest})

    assert revoked.status_code == 200 and revoked.json()["decision"] == "rejected"
    assert again.status_code == 409 and "not pending" in again.json()["detail"]


def test_a_write_from_a_page_on_another_origin_is_refused(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings)), headers=OPERATOR_HEADERS) as client:
        foreign = client.post(
            "/incidents/run",
            json={"scenario_id": "nope"},
            headers={"Origin": "https://evil.example"},
        )
        dev_server = client.post(
            "/incidents/run",
            json={"scenario_id": "nope"},
            headers={"Origin": "http://localhost:5173"},
        )
        read = client.get("/health", headers={"Origin": "https://evil.example"})

    assert foreign.status_code == 403
    assert dev_server.status_code == 404  # allowed through to the endpoint (unknown scenario)
    assert read.status_code == 200  # reads are not state-changing


def test_only_loopback_host_names_are_served_by_default() -> None:
    assert origin_allowed(None, ["localhost"])  # not a browser: no Origin header
    assert origin_allowed("http://[::1]:5173", ["[::1]"])
    assert not origin_allowed("http://localhost.evil.example", ["localhost"])
    assert not origin_allowed("file://localhost", ["localhost"])


def test_the_ipv6_loopback_host_is_allowed(settings: Settings) -> None:
    app = create_app(Runtime(settings), allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    with TestClient(app, base_url="http://[::1]:8000") as client:
        assert client.get("/health").status_code == 200
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/health").status_code == 400


def test_cancelling_a_run_that_waits_for_approval_expires_its_request(settings: Settings) -> None:
    runtime = Runtime(settings)
    with TestClient(create_app(runtime), headers=OPERATOR_HEADERS) as client:
        run_id = client.post(
            "/incidents/run",
            json={"scenario_id": "bad-deployment", "config": {"approval_mode": "manual"}},
        ).json()["run_id"]
        pending = wait_for(lambda: client.get("/approvals/pending").json())[0]

        cancelled = client.post(f"/runs/{run_id}/cancel")
        record = wait_for(
            lambda: (body := client.get(f"/runs/{run_id}").json())["status"] != "running" and body
        )["record"]
        late = client.post(
            f"/approvals/{pending['proposal_id']}",
            json={"decision": "approved", "proposal_digest": pending["proposal_digest"]},
        )
        unknown = client.post("/runs/run-does-not-exist/cancel")

    assert cancelled.status_code == 202
    assert record["state"] == "FAILED" and record["outcome_note"] == "run cancelled"
    assert late.status_code == 409  # nobody can approve the cancelled run's proposal
    kinds = [e.kind for e in runtime.approvals.events(pending["proposal_id"])]
    assert kinds[:2] == ["requested", "expired"] and kinds[-1] == "refused"
    assert unknown.status_code == 404


def test_a_generated_token_is_saved_for_this_user_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECAP_OPERATOR_TOKEN", raising=False)

    first = OperatorAuth.from_environment(tmp_path)
    second = OperatorAuth.from_environment(tmp_path)  # the next start reads the same file

    token_file = tmp_path / "operator-token"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    token = token_file.read_text().strip()
    assert len(token) >= 40
    for auth in (first, second):
        assert auth._token == token.encode()
